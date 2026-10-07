"""PostgreSQL adapter: server-side cursor reads, quantile / ctid splits, COPY writes, ON CONFLICT upserts."""
from __future__ import annotations

import datetime as dt
import decimal
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pyarrow as pa

from ..arrowconv import dedupe_last, last_value, rows_to_table_lenient, table_rows
from ..ddl import TargetOptions, create_table_sql, qi, qualified, split_name
from ..model import Batch, ReadRequest, SplitSpec
from ..types import ColSpec, clean, normalize
from .base import (
    Adapter, SqlDialect, Writer, build_count_sql, build_minmax_sql, build_probe_sql, build_read_sql, equal_bounds,
)

_OID_TYPES = {
    "int2": "SMALLINT", "int4": "INTEGER", "int8": "BIGINT", "float4": "FLOAT", "float8": "DOUBLE", "text": "TEXT",
    "bool": "BOOLEAN", "date": "DATE", "timestamp": "TIMESTAMP", "timestamptz": "TIMESTAMPTZ", "bytea": "BLOB",
    "json": "TEXT", "jsonb": "TEXT", "uuid": "VARCHAR", "oid": "BIGINT",
}


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

    def server_version(self) -> int:
        return int(self._rows("SHOW server_version_num")[0][0])

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
        return PgWriter(self._open(), table, cols, keys if mode == "upsert" else [])


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
