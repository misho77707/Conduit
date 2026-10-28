"""MySQL / MariaDB adapter, with two drivers for moving rows:

pymysql (classic): unbuffered (streaming) reads, multi-row INSERT / ON DUPLICATE KEY UPDATE writes.
fast: connectorx reads (Arrow from Rust) of table sources: JSON is read as CAST(... AS CHAR) and TIMESTAMP as
UNIX_TIMESTAMP(...) so neither depends on the session (connectorx can't SET time_zone); TIME, BIT, spatial types and
DECIMAL with more than 10 decimals keep the source on pymysql. Writes are LOAD DATA LOCAL INFILE of CSV that Arrow
builds (no Python per row), one temp file per batch; LOAD DATA LOCAL turns errors into warnings, so any warning rolls
the batch back and it is bisected to its bad rows like an INSERT error. Upserts load a temp table, then INSERT ...
SELECT ... ON DUPLICATE KEY UPDATE.
"""
from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator
from typing import Any

import pyarrow as pa
import pymysql
import pymysql.cursors
from pymysql.constants import FIELD_TYPE as FT

from ..arrowconv import dedupe_last, from_driver, last_value, rows_to_table_lenient, table_rows
from ..ddl import TargetOptions, create_table_sql, qi, qualified, split_name
from ..drivers import CapabilityError
from ..model import Batch, ReadRequest, SplitSpec
from ..types import ColSpec, clean, normalize, to_arrow
from .base import (
    Adapter, SqlDialect, Writer, build_count_sql, build_minmax_sql, build_probe_sql, build_read_sql, is_utc,
)

_UTC = dt.timezone.utc
# session settings: UTC so TIMESTAMP columns behave the same everywhere; long write timeout so a slow consumer does not drop the stream
_INIT = "SET time_zone = '+00:00', net_write_timeout = 900, net_read_timeout = 900"


def to_bind(v: Any) -> Any:
    if isinstance(v, dt.datetime) and v.tzinfo is not None:
        return v.astimezone(_UTC).replace(tzinfo=None)
    return v


_SQL_DEFAULT = re.compile(r"^('.*'|-?[\d.]+|\w+\(.*\)|current_timestamp|current_date|null)$", re.I | re.S)


def _default_expr(d: str | None, extra: str | None) -> str | None:
    """information_schema.columns.column_default as a SELECT-able expression.

    MySQL 8 reports constant defaults as bare text (abc, 30240) and flags expressions DEFAULT_GENERATED;
    MariaDB reports SQL ('abc', 30240, current_timestamp()) and the string NULL for "no default".
    """
    extra = (extra or "").lower()
    if "auto_increment" in extra:
        return "auto_increment"
    if d is None or d.upper() == "NULL":
        return None
    if "default_generated" in extra or _SQL_DEFAULT.match(d):
        return d
    return "'" + d.replace("\\", "\\\\").replace("'", "''") + "'"


# connectorx reads: pymysql FIELD_TYPE -> how. TIME (a duration to pymysql, a time of day to connectorx), BIT,
# GEOMETRY and anything unlisted keep the source on pymysql.
_CX_DIRECT = {FT.TINY, FT.SHORT, FT.LONG, FT.INT24, FT.LONGLONG, FT.FLOAT, FT.DOUBLE, FT.NEWDECIMAL, FT.DATE, FT.DATETIME,
              FT.YEAR, FT.VARCHAR, FT.VAR_STRING, FT.STRING, FT.ENUM, FT.SET, FT.TINY_BLOB, FT.MEDIUM_BLOB, FT.LONG_BLOB,
              FT.BLOB}


class LoadDataRowError(ValueError):
    """LOAD DATA LOCAL warned about the batch (it never errors): bisected to its bad rows like an INSERT error."""


def _sql_literals(sql: str, params: list[Any]) -> str:
    from pymysql.converters import escape_item

    return sql % tuple(escape_item(to_bind(p), "utf8mb4") for p in params)


def load_data_text(table: pa.Table) -> bytes:
    """CSV for LOAD DATA (FIELDS TERMINATED BY ',' OPTIONALLY ENCLOSED BY '"' ESCAPED BY ''), built by Arrow:
    text quoted with "" inside, NULL unquoted, booleans 1 / 0, time-zone-aware timestamps as UTC wall time,
    binary as hex (loaded through UNHEX)."""
    import pyarrow.compute as pc

    cols = []
    for col in table.columns:
        col = col.combine_chunks()
        t = col.type
        if pa.types.is_timestamp(t) and t.tz:
            col = col.cast(pa.timestamp(t.unit))
        if pa.types.is_boolean(t):
            s = pc.if_else(col, "1", "0")
        elif pa.types.is_binary(t) or pa.types.is_large_binary(t):
            s = pa.array([None if v is None else v.hex() for v in col.to_pylist()], pa.string())
        else:
            s = col.cast(pa.string())
        if pa.types.is_string(t) or pa.types.is_large_string(t):
            s = pc.binary_join_element_wise('"', pc.replace_substring(s, '"', '""'), '"', "")
        cols.append(pc.fill_null(s, "NULL"))
    lines = pc.binary_join_element_wise(*cols, ",") if len(cols) > 1 else cols[0]
    lines = pc.binary_join_element_wise(lines, "", "\n")
    if isinstance(lines, pa.ChunkedArray):
        lines = lines.combine_chunks()
    offsets = lines.buffers()[1]
    import numpy as np

    o = np.frombuffer(offsets, dtype=np.int32)
    return lines.buffers()[2].to_pybytes()[o[lines.offset]:o[lines.offset + len(lines)]]


def _logical_from_field(col: tuple, binary: bool) -> str:
    code, size, prec, scale = col[1], col[3], col[4], col[5]
    if code in (FT.TINY,):
        return "BOOLEAN" if size == 1 else "SMALLINT"
    if code == FT.SHORT:
        return "SMALLINT"
    if code in (FT.LONG, FT.INT24):
        return "INTEGER"
    if code == FT.LONGLONG:
        return "BIGINT"
    if code == FT.FLOAT:
        return "FLOAT"
    if code == FT.DOUBLE:
        return "DOUBLE"
    if code in (FT.NEWDECIMAL, FT.DECIMAL):
        p = min(int(prec or 38), 38)
        s = min(int(scale or 0), p)
        return f"DECIMAL({p},{s})" if prec else "DECIMAL(38,10)"
    if code == FT.DATE:
        return "DATE"
    if code in (FT.DATETIME, FT.TIMESTAMP):
        return "TIMESTAMP"
    if code in (FT.TINY_BLOB, FT.MEDIUM_BLOB, FT.LONG_BLOB, FT.BLOB):
        return "BLOB" if binary else "TEXT"
    if code == FT.JSON:
        return "TEXT"
    return "VARCHAR"


class MySQLAdapter(Adapter):
    dialect = "mysql"
    sql = SqlDialect("mysql", "%s", True)
    max_writers = 8

    def _rows(self, sql: str, params: list[Any] | None = None) -> list[tuple]:
        conn = self.c._connect(init_command=_INIT)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return list(cur.fetchall()) if cur.description else []
        finally:
            conn.close()

    def _schema(self, table: str) -> tuple[str | None, str]:
        return split_name(table)

    # ---- drivers -----------------------------------------------------------------
    def driver_problem(self, driver: str, side: str, ctx: Any) -> str | None:
        if driver != "fast":
            return None
        if side == "target":
            on = self._rows("SHOW GLOBAL VARIABLES LIKE 'local_infile'")
            if not on or str(on[0][1]).upper() not in ("ON", "1"):
                return f"LOAD DATA needs local_infile=ON on the server (it is {on[0][1] if on else 'unknown'})"
            return None
        if self.c.cx_uri() is None:
            return "connectorx can't use this connection's TLS setting"
        if ctx.query:
            return "custom SQL may depend on the session time zone, which connectorx can't set"
        for name, (kind, shown) in self._cx_plan(ctx).items():
            if kind is None:
                return f"column `{name}` is `{shown}`, which the connectorx read path here doesn't convert"
        return None

    def _cx_plan(self, req: ReadRequest) -> dict[str, tuple[str | None, str]]:
        """name -> (direct | json | epoch, or None if connectorx can't read it as pymysql does; the column type)."""
        key = (req.table, tuple(c.name for c in req.columns))
        cached = getattr(self, "_plans", {}).get(key)
        if cached is not None:
            return cached
        schema, name = self._schema(req.table or "")
        shown = dict(self._rows("select column_name, column_type from information_schema.columns "
                                "where table_schema = COALESCE(%s, DATABASE()) and table_name = %s", [schema, name]))
        conn = self.c._connect(init_command=_INIT)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {', '.join(qi('mysql', c.name) for c in req.columns)} FROM {qualified('mysql', req.table or '')} LIMIT 0")
                desc = list(cur.description or [])
        finally:
            conn.close()
        plan = {}
        for c, d in zip(req.columns, desc):
            code = d[1]
            m = re.match(r"decimal\((\d+),(\d+)\)", str(shown.get(c.name, "")).lower())
            if code == FT.JSON:
                kind = "json"
            elif code == FT.TIMESTAMP:
                kind = "epoch"
            elif code == FT.NEWDECIMAL and (not m or int(m.group(2)) > 10):  # connectorx reads 10 decimals at most
                kind = None
            else:
                kind = "direct" if code in _CX_DIRECT else None
            plan[c.name] = (kind, shown.get(c.name, str(code)))
        if not hasattr(self, "_plans"):
            self._plans = {}
        self._plans[key] = plan
        return plan

    def _read_driver(self, req: ReadRequest) -> str:
        if self.driver_for("source") is None and self.driver is not None:
            self.settle("source", req)  # previews / Compare: the connection's own choice
        return self.driver_for("source") or "pymysql"

    def _read_cx(self, req: ReadRequest, split: SplitSpec, batch_rows: int) -> Iterator[Batch]:
        import connectorx as cx

        plan = self._cx_plan(req)
        exprs = {n: (f"CAST({qi('mysql', n)} AS CHAR)" if k == "json" else f"UNIX_TIMESTAMP({qi('mysql', n)})")
                 for n, (k, _) in plan.items() if k in ("json", "epoch")}
        epoch = [i for i, c in enumerate(req.columns) if plan[c.name][0] == "epoch"]
        sql, params = build_read_sql(self.sql, req, split, exprs=exprs)
        try:
            reader = cx.read_sql(self.c.cx_uri(), _sql_literals(sql, params), return_type="arrow_stream", batch_size=batch_rows)
            for rb in reader:
                if rb.num_rows == 0:
                    continue
                t = pa.Table.from_batches([rb])
                for i in epoch:  # UNIX_TIMESTAMP seconds (decimal / integer) -> the UTC wall time pymysql reads
                    import pyarrow.compute as pc

                    secs = t.column(i).combine_chunks()
                    micros = pc.multiply(secs.cast(pa.decimal128(18, 6)), pa.scalar(1_000_000, pa.decimal128(7, 0)))
                    t = t.set_column(i, t.field(i).name, micros.cast(pa.int64()).cast(pa.timestamp("us")))
                table, rejects = from_driver(t, req.columns)
                resume = last_value(table, split.column) if split.kind == "range" and split.column and table.num_rows else None
                yield Batch(table, resume, rejects)
        except BaseException as e:  # connectorx panics (zero dates ...) arrive as a BaseException
            if isinstance(e, (GeneratorExit, KeyboardInterrupt, SystemExit)):
                raise
            if type(e).__name__ == "PanicException" or "not implemented" in str(e).lower():
                raise CapabilityError("source", f"connectorx: {' '.join(str(e).split())[:300]}") from e
            raise

    # ---- ClickHouse direct pull --------------------------------------------------
    ch_function = "mysql"
    ch_limit_pushdown = False  # ClickHouse sends the SELECT without its LIMIT and reads until it has enough
    ch_settings = {"mysql_datatypes_support_level": "decimal,datetime64"}  # else DECIMAL is text, DATETIME(6) seconds

    def ch_table_args(self, table: str) -> dict[str, str]:
        schema, name = self._schema(table)
        return {"database": schema or str(self.c.config.get("database") or ""), "table": name}

    def ch_pull_problem(self, req: ReadRequest) -> str | None:
        floats = next((c.name for c in req.columns if clean(c.logical) in ("FLOAT", "DOUBLE")), None)
        if floats:  # ClickHouse 24.8's MySQL reader parses MySQL's text form itself (precise_float_parsing doesn't reach it)
            return (f"column `{floats}` is a floating-point column, which ClickHouse's MySQL reader can get one unit off "
                    "in the last digit (0.375 arrives as 0.37500000000000006)")
        schema, name = self._schema(req.table or "")
        stamps = {r[0] for r in self._rows("select column_name from information_schema.columns where table_schema = "
                                           "COALESCE(%s, DATABASE()) and table_name = %s and data_type = 'timestamp'",
                                           [schema, name])}
        hit = next((c.name for c in req.columns if c.name in stamps), None)
        if hit is None:
            return None
        # TIMESTAMP arrives in the session's zone and ClickHouse can't set it; Conduit's sessions use UTC (_INIT).
        conn = self.c._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT @@session.time_zone, @@system_time_zone")
                zone, system = cur.fetchone()
        finally:
            conn.close()
        zone = system if str(zone).upper() == "SYSTEM" else zone
        if not is_utc(zone):
            return f"column `{hit}` is TIMESTAMP and the server's time zone is {zone}: ClickHouse would read its local times as UTC"
        return None

    # ---- schema ------------------------------------------------------------------
    def table_columns(self, table: str) -> list[ColSpec]:
        schema, name = self._schema(table)
        rows = self._rows(
            "select column_name, column_type, is_nullable, column_default, extra from information_schema.columns "
            "where table_schema = COALESCE(%s, DATABASE()) and table_name = %s order by ordinal_position", [schema, name])
        return [ColSpec(n, normalize("mysql", t), nullable == "YES", default=_default_expr(d, extra))
                for n, t, nullable, d, extra in rows]

    def query_columns(self, sql: str) -> list[ColSpec]:
        conn = self.c._connect(init_command=_INIT)
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM ({sql.rstrip().rstrip(';')}) AS conduit_q LIMIT 0")
                fields = getattr(getattr(cur, "_result", None), "fields", None) or []
                return [ColSpec(d[0], _logical_from_field(d, bool(fields and fields[i].charsetnr == 63)), True)
                        for i, d in enumerate(cur.description or [])]
        finally:
            conn.close()

    def is_unique(self, table: str, column: str) -> bool:
        schema, name = self._schema(table)
        return bool(self._rows(
            "select index_name from information_schema.statistics "
            "where table_schema = COALESCE(%s, DATABASE()) and table_name = %s and non_unique = 0 "
            "group by index_name having count(*) = 1 and min(column_name) = %s", [schema, name, column]))

    # ---- source ------------------------------------------------------------------
    def estimate_rows(self, req: ReadRequest, exact: bool = False) -> tuple[int | None, bool]:
        if exact:
            sql, params = build_count_sql(self.sql, req)
            return int(self._rows(sql, params)[0][0]), True
        unfiltered = not req.query and not req.where.strip() and (req.mode == "full" or req.lo is None and req.hi is None)
        if unfiltered:
            schema, name = self._schema(req.table or "")
            rows = self._rows("select table_rows from information_schema.tables "
                              "where table_schema = COALESCE(%s, DATABASE()) and table_name = %s", [schema, name])
            if rows and rows[0][0] is not None:
                return int(rows[0][0]), False
        sql, params = build_probe_sql(self.sql, req)
        conn = self.c._connect(init_command=_INIT)
        try:
            with conn.cursor() as cur:
                cur.execute("EXPLAIN " + sql, params)
                names = [d[0].lower() for d in cur.description]
                idx = names.index("rows")
                vals = [int(r[idx]) for r in cur.fetchall() if r[idx] is not None]
                return (max(vals) if vals else None), False
        except pymysql.MySQLError:
            return None, False
        finally:
            conn.close()

    def key_range(self, req: ReadRequest) -> tuple[Any, Any] | None:
        sql, params = build_minmax_sql(self.sql, req)
        mn, mx, cnt = self._rows(sql, params)[0]
        return None if mn is None or not cnt else (mn, mx)

    def read_batches(self, req: ReadRequest, split: SplitSpec, batch_rows: int) -> Iterator[Batch]:
        if self._read_driver(req) == "fast":
            yield from self._read_cx(req, split, batch_rows)
            return
        sql, params = build_read_sql(self.sql, req, split)
        conn = self.c._connect(cursorclass=pymysql.cursors.SSCursor, autocommit=False, read_timeout=3600, init_command=_INIT)
        try:
            with conn.cursor() as cur:
                cur.execute(sql, [to_bind(p) for p in params])
                while True:
                    rows = cur.fetchmany(batch_rows)
                    if not rows:
                        break
                    table, rejects = rows_to_table_lenient(rows, req.columns)
                    yield Batch(table, last_value(table, split.column) if split.kind == "range" and split.column and table.num_rows else None, rejects)
        finally:
            conn.close()

    # ---- target ------------------------------------------------------------------
    def prepare_target(self, table: str, cols: list[ColSpec], mode: str, keys: list[str], opts: TargetOptions,
                       create: bool, execute: bool) -> list[str]:
        schema, _ = self._schema(table)
        existing = self.table_columns(table)
        stmts: list[str] = []
        if not existing and create and opts.ddl:
            stmts += opts.ddl
        elif not existing and create:
            if schema:
                stmts.append(f"CREATE DATABASE IF NOT EXISTS {qi('mysql', schema)} DEFAULT CHARSET=utf8mb4")
            stmts.append(create_table_sql("mysql", table, cols, keys, mode, opts))
        elif not existing:
            raise ValueError(f"Target table {table} does not exist")
        elif mode == "upsert":
            self._require_unique(table, keys)
        if existing and mode == "replace":
            stmts.append(f"TRUNCATE TABLE {qualified('mysql', table)}")
        if execute and stmts:
            conn = self.c._connect()
            try:
                with conn.cursor() as cur:
                    for s in stmts:
                        cur.execute(s)
            finally:
                conn.close()
        return stmts

    def _require_unique(self, table: str, keys: list[str]) -> None:
        schema, name = self._schema(table)
        rows = self._rows(
            "select index_name, column_name from information_schema.statistics "
            "where table_schema = COALESCE(%s, DATABASE()) and table_name = %s and non_unique = 0 "
            "order by index_name, seq_in_index", [schema, name])
        by_idx: dict[str, set[str]] = {}
        for idx, col in rows:
            by_idx.setdefault(idx, set()).add(col)
        if not any(s == set(keys) for s in by_idx.values()):
            raise ValueError(f"Upsert needs a PRIMARY KEY or UNIQUE index on ({', '.join(keys)}) in {table}")

    def open_writer(self, table: str, cols: list[ColSpec], mode: str, keys: list[str]) -> Writer:
        keys = keys if mode == "upsert" else []
        if self.driver_for("target") == "fast":
            return LoadDataWriter(self.c._connect(autocommit=False, init_command=_INIT, local_infile=True), table, cols, keys)
        conn = self.c._connect(autocommit=False, init_command=_INIT)
        return MySQLWriter(conn, table, cols, keys)


class MySQLWriter(Writer):
    def __init__(self, conn, table: str, cols: list[ColSpec], keys: list[str]):
        self.conn = conn
        self.keys = keys
        names = [c.name for c in cols]
        sql = (f"INSERT INTO {qualified('mysql', table)} ({', '.join(qi('mysql', n) for n in names)}) "
               f"VALUES ({', '.join(['%s'] * len(names))})")
        if keys:
            nonkey = [n for n in names if n not in keys]
            # VALUES(col) works on MySQL and MariaDB alike (the row-alias form is MySQL-only)
            sql += " ON DUPLICATE KEY UPDATE " + ", ".join(f"{qi('mysql', n)} = VALUES({qi('mysql', n)})" for n in (nonkey or keys[:1]))
        self.sql = sql

    def write(self, table: pa.Table) -> None:
        if table.num_rows == 0:
            return
        table = dedupe_last(table, self.keys)
        rows = [tuple(to_bind(v) for v in r) for r in table_rows(table)]
        try:
            with self.conn.cursor() as cur:
                cur.executemany(self.sql, rows)  # pymysql folds this into multi-row INSERT statements
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass


class LoadDataWriter(Writer):
    """LOAD DATA LOCAL INFILE of one Arrow-built CSV file per batch; one transaction per write()."""

    def __init__(self, conn, table: str, cols: list[ColSpec], keys: list[str]):
        import uuid

        self.conn, self.keys = conn, keys
        names = [c.name for c in cols]
        fq = qualified("mysql", table)
        binary = {c.name for c in cols if pa.types.is_binary(to_arrow(c.logical))}
        targets, sets = [], []
        for i, n in enumerate(names):
            if n in binary:
                targets.append(f"@b{i}")
                sets.append(f"{qi('mysql', n)} = UNHEX(@b{i})")
            else:
                targets.append(qi("mysql", n))
        into = fq
        self.after: list[str] = []
        if keys:
            self.stg = "conduit_stg_" + uuid.uuid4().hex[:8]
            with conn.cursor() as cur:
                cur.execute(f"CREATE TEMPORARY TABLE {self.stg} LIKE {fq}")
            conn.commit()
            into = self.stg
            col_sql = ", ".join(qi("mysql", n) for n in names)
            nonkey = [n for n in names if n not in keys]
            self.after = [f"INSERT INTO {fq} ({col_sql}) SELECT {col_sql} FROM {self.stg} ON DUPLICATE KEY UPDATE "
                          + ", ".join(f"{qi('mysql', n)} = VALUES({qi('mysql', n)})" for n in (nonkey or keys[:1])),
                          f"DELETE FROM {self.stg}"]
        self.sql = (f"LOAD DATA LOCAL INFILE %s INTO TABLE {into} CHARACTER SET utf8mb4 FIELDS TERMINATED BY ',' "
                    "OPTIONALLY ENCLOSED BY '\"' ESCAPED BY '' LINES TERMINATED BY '\\n' "
                    f"({', '.join(targets)})" + (" SET " + ", ".join(sets) if sets else ""))

    def write(self, table: pa.Table) -> None:
        import os
        import tempfile

        if table.num_rows == 0:
            return
        table = dedupe_last(table, self.keys)
        fd, path = tempfile.mkstemp(prefix="conduit-load-", suffix=".csv")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(load_data_text(table))
            with self.conn.cursor() as cur:
                cur.execute(self.sql, [path])
                cur.execute("SHOW WARNINGS")
                bad = [w for w in cur.fetchall() if str(w[0]).lower() != "note"]
                if bad:  # LOCAL makes errors warnings (IGNORE): refuse the batch, as an INSERT would have
                    raise LoadDataRowError(f"({bad[0][1]}) {bad[0][2]}")
                for s in self.after:
                    cur.execute(s)
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            if "local infile" in str(e).lower() or "loading local data is disabled" in str(e).lower():
                raise CapabilityError("target", f"LOAD DATA: {e}") from e
            raise
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
