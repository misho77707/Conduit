"""PostgreSQL adapter: quantile / ctid splits, ON CONFLICT upserts, and two drivers for moving rows:

psycopg (classic): server-side cursor reads, text COPY writes, every value through Python.
adbc: Arrow batches straight from binary COPY both ways (ADBC). Reads handle the column types in _ADBC_READ (numeric
arrives as text, uuid is read as text, json / jsonb are re-shaped like the classic path in arrowconv.from_driver);
any other type keeps the source on psycopg. Writes take every type: straight into the table when each column
accepts binary COPY of its Arrow type, else through a temp staging table and INSERT ... SELECT (upserts always).
"""
from __future__ import annotations

import datetime as dt
import decimal
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pyarrow as pa

from ..arrowconv import dedupe_last, from_driver, last_value, rows_to_table_lenient, table_rows
from ..ddl import TargetOptions, create_table_sql, qi, qualified, split_name
from ..drivers import CapabilityError
from ..model import Batch, ReadRequest, SplitSpec
from ..types import ColSpec, clean, normalize, to_arrow
from .base import (
    Adapter, SqlDialect, Writer, build_count_sql, build_minmax_sql, build_probe_sql, build_read_sql, equal_bounds,
    is_utc, source_sql,
)

_OID_TYPES = {
    "int2": "SMALLINT", "int4": "INTEGER", "int8": "BIGINT", "float4": "FLOAT", "float8": "DOUBLE", "text": "TEXT",
    "bool": "BOOLEAN", "date": "DATE", "timestamp": "TIMESTAMP", "timestamptz": "TIMESTAMPTZ", "bytea": "BLOB",
    "json": "TEXT", "jsonb": "TEXT", "uuid": "VARCHAR", "oid": "BIGINT",
}


# ADBC reads: pg_type.typname -> how the column is read. Anything else (arrays, interval, inet / cidr, money, time,
# timetz, xml, ranges, composites, domains ...) arrives in a shape the classic path doesn't produce: psycopg reads it.
_ADBC_READ = {
    **{t: "direct" for t in ("int2", "int4", "int8", "float4", "float8", "bool", "text", "varchar", "bpchar", "date",
                             "timestamp", "timestamptz", "bytea")},
    "numeric": "direct",  # text in an opaque extension: cast to the decimal type
    "uuid": "text",  # read as uuid::text, which is what str(UUID) gives
    "json": "json", "jsonb": "jsonb",
}
# ADBC writes: the Arrow type binary COPY sends for a column, by target typname (enums take text too).
_ADBC_DIRECT = {"int2": "int16", "int4": "int32", "int8": "int64", "float4": "float", "float8": "double", "bool": "bool",
                "text": "string", "varchar": "string", "bpchar": "string", "json": "string", "date": "date32[day]",
                "timestamp": "timestamp[us]", "timestamptz": "timestamp[us, tz=UTC]", "bytea": "binary"}
_TEXTLIKE = ("text", "varchar", "bpchar", "name")


def _stage_type(t: pa.DataType) -> str:
    """The staging column type for an Arrow type (what ADBC's binary COPY can fill)."""
    if pa.types.is_decimal(t):
        return f"numeric({t.precision},{t.scale})"
    if pa.types.is_timestamp(t):
        return "timestamptz" if t.tz else "timestamp"
    return {"int16": "smallint", "int32": "integer", "int64": "bigint", "float": "real", "double": "double precision",
            "bool": "boolean", "date32[day]": "date", "binary": "bytea"}.get(str(t), "text")


def _render(sql: str, params: list[Any]) -> str:
    """Bind parameters client-side (ADBC takes a plain statement here); %% becomes %."""
    from psycopg import sql as psql

    return sql % tuple(psql.Literal(p).as_string() for p in params)


class AdbcRowError(ValueError):
    """A data / integrity error (SQLSTATE class 22 / 23) that ADBC reported under another class: the batch is
    bisected to its bad rows, as with psycopg."""


def _adbc_error(side: str, e: Exception) -> Exception:
    """ADBC's 'not supported' (a type or feature it lacks) is a capability error: the split may retry on psycopg."""
    if type(e).__name__ == "NotSupportedError":
        return CapabilityError(side, f"ADBC: {' '.join(str(e).split())[:300]}")
    if str(getattr(e, "sqlstate", "") or "")[:2] in ("22", "23") and type(e).__name__ not in ("DataError", "IntegrityError"):
        return AdbcRowError(str(e))
    return e


def _parse_bound(text: str, logical: str) -> Any:
    """One pg_stats histogram bound (text) -> Python value of the split column's type."""
    t = clean(logical)
    if t in ("SMALLINT", "INTEGER", "BIGINT"):
        return int(text)
    if t in ("FLOAT", "DOUBLE"):
        return float(text)
    if t.startswith("DECIMAL"):
        return decimal.Decimal(text)
    if t == "DATE":
        return dt.date.fromisoformat(text)
    if t in ("TIMESTAMP", "TIMESTAMPTZ"):
        v = dt.datetime.fromisoformat(text)
        if t == "TIMESTAMPTZ" and v.tzinfo is None:
            v = v.replace(tzinfo=dt.timezone.utc)
        return v
    raise ValueError(logical)


class PostgresAdapter(Adapter):
    dialect = "postgres"
    sql = SqlDialect("postgres", "%s", True)
    max_writers = 16

    # ---- connections -------------------------------------------------------------
    def _open(self, read_only: bool = False) -> psycopg.Connection:
        conn = self.c._connect()  # autocommit connection
        conn.autocommit = False
        if read_only:
            conn.read_only = True
        return conn

    def _rows(self, sql: str, params: list[Any] | None = None) -> list[tuple]:
        with self.c._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []

    # ---- drivers -----------------------------------------------------------------
    def driver_problem(self, driver: str, side: str, ctx: Any) -> str | None:
        if driver != "adbc" or side != "source":
            return None  # ADBC writes take every type (staging + casts where binary COPY can't)
        for name, (kind, shown) in self._adbc_read_plan(ctx).items():
            if kind is None:
                return f"column `{name}` is `{shown}`, which the ADBC read path here doesn't convert"
        return None

    def probe_driver(self, driver: str) -> str | None:
        if driver != "adbc":
            return None
        import adbc_driver_postgresql.dbapi as adbc

        try:
            with adbc.connect(self.c.conninfo()) as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchall()
        except Exception as e:  # noqa: BLE001
            return str(e)
        return None

    def _adbc_read_plan(self, req: ReadRequest) -> dict[str, tuple[str | None, str]]:
        """name -> (how ADBC reads it, or None if it can't; the type as Postgres shows it), from a zero-row probe."""
        key = (req.table, req.query, tuple(c.name for c in req.columns))
        cached = getattr(self, "_plans", {}).get(key)
        if cached is not None:
            return cached
        cols = ", ".join(qi("postgres", c.name) for c in req.columns)
        with self.c._connect() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {cols} FROM {source_sql(self.sql, req)} LIMIT 0", [])
            oids = [d.type_code for d in cur.description or []]
            cur.execute("SELECT oid, typname, typtype, format_type(oid, NULL) FROM pg_type WHERE oid = ANY(%s)", [oids])
            info = {r[0]: r[1:] for r in cur.fetchall()}
        plan = {}
        for c, oid in zip(req.columns, oids):
            typname, typtype, shown = info.get(oid, ("?", "?", f"oid {oid}"))
            kind = "direct" if typtype == "e" else _ADBC_READ.get(typname)
            plan[c.name] = (kind, shown)
        if not hasattr(self, "_plans"):
            self._plans = {}
        self._plans[key] = plan
        return plan

    def _read_driver(self, req: ReadRequest) -> str:
        if self.driver_for("source") is None and self.driver is not None:
            self.settle("source", req)  # previews / Compare: the connection's own choice
        return self.driver_for("source") or "psycopg"

    def server_version(self) -> int:
        return int(self._rows("SHOW server_version_num")[0][0])

    # ---- ClickHouse direct pull --------------------------------------------------
    ch_function = "postgresql"

    def ch_table_args(self, table: str) -> dict[str, str]:
        schema, name = split_name(table)
        return {"schema": schema or "public", "table": name}

    def ch_pull_problem(self, req: ReadRequest) -> str | None:
        tz_cols = [c.name for c in req.columns if clean(c.logical) == "TIMESTAMPTZ"]
        if tz_cols:
            # ClickHouse reads timestamptz as text in the session's zone and drops the offset; it can't set the zone.
            # (Read on Conduit's own session: the collection's user is assumed to get the same server default.)
            zone = self._rows("SHOW timezone")[0][0]
            if not is_utc(zone):
                return (f"column `{tz_cols[0]}` is timestamptz and the server's time zone is {zone}: ClickHouse would read "
                        "its local times as UTC")
        return None

    def ch_expr(self, col: ColSpec, ident: str) -> str | None:
        if clean(col.logical) == "BLOB":
            return f"unhex(substring({ident}, 3))"  # bytea arrives as its hex text, '\\x…'
        return None

    # ---- schema ------------------------------------------------------------------
    def table_columns(self, table: str) -> list[ColSpec]:
        schema, name = split_name(table)
        rows = self._rows(
            "select a.attname, format_type(a.atttypid, a.atttypmod), not a.attnotnull, "
            "case when a.attidentity <> '' then 'identity' else pg_get_expr(d.adbin, d.adrelid) end "
            "from pg_attribute a join pg_class c on c.oid = a.attrelid join pg_namespace n on n.oid = c.relnamespace "
            "left join pg_attrdef d on d.adrelid = a.attrelid and d.adnum = a.attnum "
            "where n.nspname = %s and c.relname = %s and a.attnum > 0 and not a.attisdropped order by a.attnum",
            [schema or "public", name])
        return [ColSpec(a, normalize("postgres", t), nullable, default=d) for a, t, nullable, d in rows]

    def query_columns(self, sql: str) -> list[ColSpec]:
        with self.c._connect() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT * FROM ({sql.rstrip().rstrip(';')}) AS conduit_q LIMIT 0")
            out = []
            for col in cur.description or []:
                info = conn.adapters.types.get(col.type_code)
                tname = info.name if info else "text"
                if tname == "numeric":
                    logical = f"DECIMAL({min(col.precision, 38)},{col.scale})" if col.precision else "DECIMAL(38,10)"
                elif tname in ("varchar", "bpchar"):
                    logical = f"VARCHAR({col.display_size})" if col.display_size and col.display_size > 0 else "VARCHAR"
                else:
                    logical = _OID_TYPES.get(tname, "VARCHAR")
                out.append(ColSpec(col.name, logical, True))
            return out

    def is_unique(self, table: str, column: str) -> bool:
        schema, name = split_name(table)
        return bool(self._rows(
            "select 1 from pg_index i join pg_class c on c.oid = i.indrelid join pg_namespace n on n.oid = c.relnamespace "
            "join pg_attribute a on a.attrelid = c.oid and a.attnum = i.indkey[0] "
            "where n.nspname = %s and c.relname = %s and a.attname = %s and i.indisunique and i.indnkeyatts = 1 "
            "and i.indpred is null limit 1", [schema or "public", name, column]))

    # ---- source ------------------------------------------------------------------
    def estimate_rows(self, req: ReadRequest, exact: bool = False) -> tuple[int | None, bool]:
        unfiltered = not req.query and not req.where.strip() and (req.mode == "full" or req.lo is None and req.hi is None)
        if exact:
            sql, params = build_count_sql(self.sql, req)
            return int(self._rows(sql, params)[0][0]), True
        if unfiltered:
            schema, name = split_name(req.table or "")
            rows = self._rows("select c.reltuples::bigint from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                              "where n.nspname = %s and c.relname = %s", [schema or "public", name])
            if rows and rows[0][0] is not None and rows[0][0] >= 0:
                return int(rows[0][0]), False
        sql, params = build_probe_sql(self.sql, req)
        try:
            plan = self._rows("EXPLAIN (FORMAT JSON) " + sql, params)[0][0]
            return int(plan[0]["Plan"]["Plan Rows"]), False
        except psycopg.Error:
            return None, False

    def key_range(self, req: ReadRequest) -> tuple[Any, Any] | None:
        sql, params = build_minmax_sql(self.sql, req)
        mn, mx, cnt = self._rows(sql, params)[0]
        return None if mn is None or not cnt else (mn, mx)

    def plan_splits(self, req: ReadRequest, n: int) -> list[SplitSpec]:
        if req.split_col == "ctid":
            return self._ctid_splits(req, n)
        if n <= 1 or not req.split_col or not self.is_splittable(req.split_col_logical):
            return super().plan_splits(req, n)
        rng = self.key_range(req)
        if rng is None:
            return super().plan_splits(req, n)
        mn, mx = rng
        hi = self.after(mx)
        bounds = self._histogram_bounds(req, n, mn, hi) or equal_bounds(mn, hi, n)
        return self.ranges_from_bounds(req, bounds)

    def _histogram_bounds(self, req: ReadRequest, n: int, mn: Any, hi: Any) -> list[Any] | None:
        """Equal-population boundaries from the planner's own statistics, so skewed keys still split evenly."""
        if req.query or not req.table or not req.split_col_logical:
            return None
        schema, name = split_name(req.table)
        try:
            rows = self._rows("select histogram_bounds::text::text[] from pg_stats "
                              "where schemaname = %s and tablename = %s and attname = %s",
                              [schema or "public", name, req.split_col])
            if not rows or not rows[0][0]:
                return None
            hist = [_parse_bound(x, req.split_col_logical) for x in rows[0][0]]
        except (psycopg.Error, ValueError, TypeError):
            return None
        inner = sorted({b for b in hist if mn < b < hi})
        if len(inner) < 2:
            return None
        k = min(n, len(inner) + 1)
        cuts = [inner[round(i * len(inner) / k)] for i in range(1, k)]
        out = [mn]
        for c in cuts:
            if c > out[-1]:
                out.append(c)
        out.append(hi)
        return out if len(out) > 2 else None

    def _ctid_splits(self, req: ReadRequest, n: int) -> list[SplitSpec]:
        if req.query or not req.table:
            self.notes.append("Custom SQL sources cannot be split by physical block; reading in one piece.")
            return [SplitSpec(0, "all rows", "full")]
        if self.server_version() < 140000:
            self.notes.append("Block-range splits need PostgreSQL 14+ (TID range scans); reading in one piece.")
            return [SplitSpec(0, "all rows", "full")]
        schema, name = split_name(req.table)
        rows = self._rows(
            "select pg_relation_size(c.oid) / current_setting('block_size')::int from pg_class c "
            "join pg_namespace n on n.oid = c.relnamespace where n.nspname = %s and c.relname = %s",
            [schema or "public", name])
        blocks = int(rows[0][0]) if rows else 0
        if n <= 1 or blocks < 2:
            return [SplitSpec(0, "all rows", "full")]
        est, _ = self.estimate_rows(ReadRequest(table=req.table))
        n = min(n, blocks)
        edges = [blocks * i // n for i in range(n + 1)]
        splits = []
        for i in range(n):
            lo, hi = edges[i], edges[i + 1]
            last = i == n - 1
            splits.append(SplitSpec(
                idx=i, label=f"blocks {lo:,} … {'end' if last else f'{hi:,}'}", kind="ctid", column="ctid",
                est_rows=(est // n if est else None), extra={"blk_lo": lo, "blk_hi": None if last else hi}))
        return splits

    def read_batches(self, req: ReadRequest, split: SplitSpec, batch_rows: int) -> Iterator[Batch]:
        extra: list[str] = []
        if split.kind == "ctid":
            lo, hi = split.extra["blk_lo"], split.extra.get("blk_hi")
            extra = [f"ctid >= '({int(lo)},0)'::tid"] + ([f"ctid < '({int(hi)},0)'::tid"] if hi is not None else [])
        if self._read_driver(req) == "adbc":
            yield from self._read_adbc(req, split, batch_rows, extra)
            return
        sql, params = build_read_sql(self.sql, req, split, extra=extra)
        conn = self._open(read_only=True)
        try:
            with conn.cursor(name="conduit_" + uuid.uuid4().hex[:12]) as cur:
                cur.execute(sql, params)
                while True:
                    rows = cur.fetchmany(batch_rows)
                    if not rows:
                        break
                    table, rejects = rows_to_table_lenient(rows, req.columns)
                    resume = last_value(table, split.column) if split.kind == "range" and split.column else None
                    yield Batch(table, resume, rejects)
        finally:
            conn.close()

    def _read_adbc(self, req: ReadRequest, split: SplitSpec, batch_rows: int, extra: list[str]) -> Iterator[Batch]:
        import adbc_driver_postgresql.dbapi as adbc

        plan = self._adbc_read_plan(req)
        exprs = {n: f"{qi('postgres', n)}::text" for n, (k, _) in plan.items() if k == "text"}
        json_cols = {n: k == "jsonb" for n, (k, _) in plan.items() if k in ("json", "jsonb")}
        sql, params = build_read_sql(self.sql, req, split, exprs=exprs, extra=extra)
        conn = adbc.connect(self.c.conninfo())
        try:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute(_render(sql, params))
                for rb in cur.fetch_record_batch():
                    for off in range(0, rb.num_rows, batch_rows):
                        table, rejects = from_driver(pa.Table.from_batches([rb.slice(off, batch_rows)]), req.columns, json_cols)
                        resume = last_value(table, split.column) if split.kind == "range" and split.column else None
                        yield Batch(table, resume, rejects)
        except Exception as e:  # noqa: BLE001 - re-raised, typed
            err = _adbc_error("source", e)
            if err is e:
                raise
            raise err from e
        finally:
            conn.close()

    def native_columns(self, table: str) -> dict[str, tuple[str, str, str]]:
        """name -> (typname, typtype, format_type) of an existing table's columns."""
        schema, name = split_name(table)
        rows = self._rows(
            "select a.attname, t.typname, t.typtype, format_type(a.atttypid, a.atttypmod) from pg_attribute a "
            "join pg_class c on c.oid = a.attrelid join pg_namespace n on n.oid = c.relnamespace "
            "join pg_type t on t.oid = a.atttypid "
            "where n.nspname = %s and c.relname = %s and a.attnum > 0 and not a.attisdropped", [schema or "public", name])
        return {r[0]: (r[1], r[2], r[3]) for r in rows}

    # ---- target ------------------------------------------------------------------
    def prepare_target(self, table: str, cols: list[ColSpec], mode: str, keys: list[str], opts: TargetOptions,
                       create: bool, execute: bool) -> list[str]:
        schema, _ = split_name(table)
        existing = self.table_columns(table)
        stmts: list[str] = []
        if not existing and create and opts.ddl:
            stmts += opts.ddl
        elif not existing and create:
            if schema:
                stmts.append(f"CREATE SCHEMA IF NOT EXISTS {qi('postgres', schema)}")
            stmts.append(create_table_sql("postgres", table, cols, keys, mode, opts))
        elif not existing:
            raise ValueError(f"Target table {table} does not exist")
        elif mode == "upsert":
            self._require_unique(table, keys)
        if existing and mode == "replace":
            stmts.append(f"TRUNCATE TABLE {qualified('postgres', table)}")
        if execute and stmts:
            with self.c._connect() as conn:
                with conn.transaction():
                    for s in stmts:
                        conn.execute(s)
        return stmts

    def _require_unique(self, table: str, keys: list[str]) -> None:
        schema, name = split_name(table)
        rows = self._rows(
            "select array_agg(a.attname::text order by k.ord) from pg_index i "
            "join pg_class c on c.oid = i.indrelid join pg_namespace n on n.oid = c.relnamespace "
            "cross join lateral unnest(i.indkey::int2[]) with ordinality as k(attnum, ord) "
            "join pg_attribute a on a.attrelid = c.oid and a.attnum = k.attnum "
            "where n.nspname = %s and c.relname = %s and i.indisunique and i.indpred is null "
            "group by i.indexrelid", [schema or "public", name])
        if not any(set(r[0]) == set(keys) for r in rows):
            raise ValueError(f"Upsert needs a PRIMARY KEY or UNIQUE index on ({', '.join(keys)}) in {table}")

    def open_writer(self, table: str, cols: list[ColSpec], mode: str, keys: list[str]) -> Writer:
        keys = keys if mode == "upsert" else []
        if self.driver_for("target") == "adbc":
            return PgAdbcWriter(self, table, cols, keys)
        return PgWriter(self._open(), table, cols, keys)


class PgWriter(Writer):
    def __init__(self, conn: psycopg.Connection, table: str, cols: list[ColSpec], keys: list[str]):
        self.conn = conn
        self.fq = qualified("postgres", table)
        self.cols = [c.name for c in cols]
        self.col_sql = ", ".join(qi("postgres", c) for c in self.cols)
        self.keys = keys
        self.stg = "conduit_stg_" + uuid.uuid4().hex[:8]

    def _copy(self, cur: psycopg.Cursor, target: str, table: pa.Table) -> None:
        with cur.copy(f"COPY {target} ({self.col_sql}) FROM STDIN") as cp:
            for row in table_rows(table):
                cp.write_row(row)

    def write(self, table: pa.Table) -> None:
        if table.num_rows == 0:
            return
        with self.conn.transaction(), self.conn.cursor() as cur:
            if not self.keys:
                self._copy(cur, self.fq, table)
                return
            table = dedupe_last(table, self.keys)
            cur.execute(f"CREATE TEMP TABLE {self.stg} (LIKE {self.fq} INCLUDING DEFAULTS) ON COMMIT DROP")
            self._copy(cur, self.stg, table)
            nonkey = [c for c in self.cols if c not in self.keys]
            conflict = ", ".join(qi("postgres", k) for k in self.keys)
            action = ("DO UPDATE SET " + ", ".join(f"{qi('postgres', c)} = EXCLUDED.{qi('postgres', c)}" for c in nonkey)
                      ) if nonkey else "DO NOTHING"
            cur.execute(f"INSERT INTO {self.fq} ({self.col_sql}) SELECT {self.col_sql} FROM {self.stg} "
                        f"ON CONFLICT ({conflict}) {action}")

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass


class PgAdbcWriter(Writer):
    """ADBC ingest (binary COPY of the Arrow batch). One transaction per write(), like PgWriter."""

    def __init__(self, adapter: PostgresAdapter, table: str, cols: list[ColSpec], keys: list[str]):
        import adbc_driver_postgresql.dbapi as adbc

        schema, name = split_name(table)
        self.schema, self.name, self.keys = schema or "public", name, keys
        self.fq = qualified("postgres", table)
        names = [c.name for c in cols]
        col_sql = ", ".join(qi("postgres", n) for n in names)
        natives = adapter.native_columns(table)
        arrow = {c.name: to_arrow(c.logical) for c in cols}

        def direct(n: str) -> bool:
            typname, typtype, _ = natives.get(n, ("?", "?", "?"))
            want = "string" if typtype == "e" else _ADBC_DIRECT.get(typname)
            return want is not None and (str(arrow[n]) == want or (typname == "numeric" and pa.types.is_decimal(arrow[n])))

        self.direct = not keys and all(direct(n) for n in names)
        self.conn = adbc.connect(adapter.c.conninfo())
        if self.direct:
            return
        self.stg = "conduit_stg_" + uuid.uuid4().hex[:8]
        sel = []
        for n in names:
            typname, _, shown = natives.get(n, ("?", "?", "text"))
            staged = _stage_type(arrow[n])
            # text staged into a non-text column needs an explicit cast (text -> jsonb / uuid / enum / arrays ...);
            # everything else is an assignment cast, which (unlike an explicit one) refuses over-long values
            sel.append(f"{qi('postgres', n)}::{shown}" if staged == "text" and typname not in _TEXTLIKE else qi("postgres", n))
        insert = f"INSERT INTO {self.fq} ({col_sql}) SELECT {', '.join(sel)} FROM {self.stg}"
        if keys:
            nonkey = [n for n in names if n not in keys]
            conflict = ", ".join(qi("postgres", k) for k in keys)
            action = ("DO UPDATE SET " + ", ".join(f"{qi('postgres', n)} = EXCLUDED.{qi('postgres', n)}" for n in nonkey)
                      ) if nonkey else "DO NOTHING"
            insert += f" ON CONFLICT ({conflict}) {action}"
        self.insert = insert
        try:
            with self.conn.cursor() as cur:
                cur.execute(f"CREATE TEMP TABLE {self.stg} ("
                            + ", ".join(f"{qi('postgres', n)} {_stage_type(arrow[n])}" for n in names)
                            + ") ON COMMIT DELETE ROWS")
            self.conn.commit()
        except Exception:
            self.close()
            raise

    def write(self, table: pa.Table) -> None:
        if table.num_rows == 0:
            return
        table = dedupe_last(table, self.keys)
        try:
            with self.conn.cursor() as cur:
                if self.direct:
                    cur.adbc_ingest(self.name, table, mode="append", db_schema_name=self.schema)
                else:
                    cur.adbc_ingest(self.stg, table, mode="append", temporary=True)
                    cur.execute(self.insert)
            self.conn.commit()
        except Exception as e:  # noqa: BLE001 - re-raised, typed
            try:
                self.conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            err = _adbc_error("target", e)
            if err is e:
                raise
            raise err from e

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
