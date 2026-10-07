"""ClickHouse adapter over the HTTP interface: Arrow streams in and out, partition-aware splits.

Time values always move as UTC instants: DateTime columns with no explicit time zone are read as their
UTC value, and bounds are sent as UTC literals.
"""
from __future__ import annotations

import datetime as dt
import decimal
import re
import threading
from collections.abc import Iterator
from typing import Any

import pyarrow as pa

from ..arrowconv import conform, last_value
from ..ddl import TargetOptions, create_table_sql, qi, qualified, split_name
from ..model import Batch, ReadRequest, SplitSpec
from ..types import ColSpec, normalize
from .base import Adapter, SqlDialect, Writer, build_count_sql, build_minmax_sql, build_probe_sql, build_read_sql

_UTC = dt.timezone.utc
_WRAP = re.compile(r"^(?:Nullable|LowCardinality)\((.*)\)$")


def lit(v: Any) -> str:
    """Python value -> ClickHouse literal (bounds and resume keys only)."""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float, decimal.Decimal)):
        return repr(v) if isinstance(v, float) else str(v)
    if isinstance(v, dt.datetime):
        if v.tzinfo is not None:
            v = v.astimezone(_UTC).replace(tzinfo=None)
        return f"toDateTime64('{v.strftime('%Y-%m-%d %H:%M:%S.%f')}', 6, 'UTC')"
    if isinstance(v, dt.date):
        return f"toDate('{v.isoformat()}')"
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _base(t: str) -> str:
    while True:
        m = _WRAP.match(t)
        if not m:
            return t
        t = m.group(1)


def expr_for(col: str, ch_type: str) -> str | None:
    """SELECT expression for types Arrow cannot carry as the logical type we want."""
    b = _base(ch_type)
    q = qi("clickhouse", col)
    if b.startswith(("UUID", "IPv4", "IPv6", "Enum", "Int128", "UInt128", "Int256", "UInt256")):
        return f"toString({q})"
    if b.startswith("FixedString"):
        return f"replaceRegexpOne(toString({q}), '\\\\0+$', '')"
    if b.startswith(("Array", "Map", "Tuple", "Nested", "JSON", "Object", "Variant", "Dynamic")):
        return f"toJSONString({q})"
    return None


class ClickHouseAdapter(Adapter):
    dialect = "clickhouse"
    sql = SqlDialect("clickhouse", "%s", True)  # %s placeholders are filled with literals by us, not by the driver
    max_writers = 8

    def __init__(self, connector):
        super().__init__(connector)
        self._exprs: dict[tuple, dict[str, str]] = {}
        self._lock = threading.Lock()

    # ---- helpers -----------------------------------------------------------------
    def _db(self) -> str:
        return self.c.config.get("database") or "default"

    def _q(self, sql: str, params: list[Any] | None = None, **kw) -> list[tuple]:
        client = self.c._client(**kw)
        try:
            return client.query(_fill(sql, params or [])).result_rows
        finally:
            client.close()

    def after(self, v: Any) -> Any:
        if isinstance(v, dt.datetime):  # DateTime is second-granular; a whole second above the max is always enough
            return v + (dt.timedelta(seconds=1) if v.microsecond == 0 else dt.timedelta(microseconds=1))
        return super().after(v)

    def ranges_from_bounds(self, req: ReadRequest, bounds: list[Any], total: int | None = None) -> list[SplitSpec]:
        if bounds and isinstance(bounds[0], dt.datetime):  # whole-second split points work for DateTime and DateTime64
            cut = [bounds[0]] + [b.replace(microsecond=0) for b in bounds[1:-1]] + [bounds[-1]]
            bounds = [b for i, b in enumerate(cut) if i == 0 or b > cut[i - 1]]
        return super().ranges_from_bounds(req, bounds, total)

    # ---- schema ------------------------------------------------------------------
    def table_columns(self, table: str) -> list[ColSpec]:
        schema, name = split_name(table)
        rows = self._q("SELECT name, type, if(default_kind = 'DEFAULT', default_expression, '') FROM system.columns "
                       "WHERE database = %s AND table = %s ORDER BY position", [schema or self._db(), name])
        return [ColSpec(n, normalize("clickhouse", t), t.startswith("Nullable") or "Nullable(" in t, default=d or None)
                for n, t, d in rows]

    def _eval_default(self, expr: str) -> Any:
        return self._q(f"SELECT {expr}")[0][0]

    def query_columns(self, sql: str) -> list[ColSpec]:
        client = self.c._client()
        try:
            res = client.query(f"SELECT * FROM ({sql.rstrip().rstrip(';')}) AS conduit_q LIMIT 0")
            return [ColSpec(n, normalize("clickhouse", t.name), "Nullable" in t.name)
                    for n, t in zip(res.column_names, res.column_types)]
        finally:
            client.close()

    def _select_exprs(self, req: ReadRequest) -> dict[str, str]:
        key = (req.table, req.query, tuple(c.name for c in req.columns))
        if key in self._exprs:
            return self._exprs[key]
        cols = ", ".join(qi("clickhouse", c.name) for c in req.columns)
        src = f"({req.query.rstrip().rstrip(';')}) AS conduit_q" if req.query else qualified("clickhouse", req.table or "")
        client = self.c._client()
        try:
            res = client.query(f"SELECT {cols} FROM {src} LIMIT 0")
            out = {}
            for c, t in zip(req.columns, res.column_types):
                e = expr_for(c.name, t.name)
                if e:
                    out[c.name] = e
        finally:
            client.close()
        with self._lock:
            self._exprs[key] = out
        return out

    # ---- source ------------------------------------------------------------------
    def estimate_rows(self, req: ReadRequest, exact: bool = False) -> tuple[int | None, bool]:
        if exact:
            sql, params = build_count_sql(self.sql, req)
            return int(self._q(sql, params)[0][0]), True
        unfiltered = not req.query and not req.where.strip() and (req.mode == "full" or req.lo is None and req.hi is None)
        if unfiltered:
            schema, name = split_name(req.table or "")
            rows = self._q("SELECT total_rows FROM system.tables WHERE database = %s AND name = %s", [schema or self._db(), name])
            if rows and rows[0][0] is not None:
                return int(rows[0][0]), False
        sql, params = build_probe_sql(self.sql, req)
        try:
            rows = self._q("EXPLAIN ESTIMATE " + sql, params)  # database, table, parts, rows, marks
            return sum(int(r[3]) for r in rows), False
        except Exception:  # noqa: BLE001 - some engines / versions cannot estimate
            return None, False

    def key_range(self, req: ReadRequest) -> tuple[Any, Any] | None:
        sql, params = build_minmax_sql(self.sql, req)
        mn, mx, cnt = self._q(sql, params)[0]  # min/max of zero rows is 0, not NULL, so the count decides
        return (mn, mx) if cnt else None

    def plan_splits(self, req: ReadRequest, n: int) -> list[SplitSpec]:
        if req.split_col == "_partition_id":
            return self._partition_splits(req, n)
        return super().plan_splits(req, n)

    def _partition_splits(self, req: ReadRequest, n: int) -> list[SplitSpec]:
        if req.query or not req.table:
            self.notes.append("Custom SQL sources cannot be split by partition; reading in one piece.")
            return [SplitSpec(0, "all rows", "full")]
        schema, name = split_name(req.table)
        parts = self._q("SELECT partition_id, sum(rows) FROM system.parts WHERE database = %s AND table = %s AND active "
                        "GROUP BY partition_id ORDER BY partition_id", [schema or self._db(), name])
        if len(parts) < 2 or n <= 1:
            if len(parts) < 2:
                self.notes.append("The table has a single partition, so there is nothing to split by partition.")
            return [SplitSpec(0, "all rows", "full")]
        total = sum(int(r[1]) for r in parts)
        target = max(1, total // n)
        groups: list[list[tuple[str, int]]] = [[]]
        acc = 0
        for pid, rows in parts:
            if groups[-1] and acc >= target and len(groups) < n:
                groups.append([])
                acc = 0
            groups[-1].append((pid, int(rows)))
            acc += int(rows)
        out = []
        for i, g in enumerate(groups):
            ids = [p for p, _ in g]
            label = ids[0] if len(ids) == 1 else f"{ids[0]} … {ids[-1]}"
            out.append(SplitSpec(idx=i, label=f"partitions {label}", kind="partition", column="_partition_id",
                                 est_rows=sum(r for _, r in g), extra={"partitions": ids}))
        return out

    def read_batches(self, req: ReadRequest, split: SplitSpec, batch_rows: int) -> Iterator[Batch]:
        extra = []
        if split.kind == "partition":
            ids = ", ".join(lit(p) for p in split.extra["partitions"])
            extra = [f"_partition_id IN ({ids})"]
        sql, params = build_read_sql(self.sql, req, split, exprs=self._select_exprs(req), extra=extra)
        sql = _fill(sql, params)
        settings = {"max_block_size": batch_rows, "max_bytes_before_external_sort": 2_000_000_000}
        client = self.c._client(read_timeout=3600, **settings)
        try:
            with client.query_arrow_stream(sql) as stream:
                for rb in stream:
                    if rb.num_rows == 0:
                        continue
                    table = conform(pa.Table.from_batches([rb]), req.columns)
                    yield Batch(table, last_value(table, split.column) if split.kind == "range" and split.column else None)
        finally:
            client.close()

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
                stmts.append(f"CREATE DATABASE IF NOT EXISTS {qi('clickhouse', schema)}")
            stmts.append(create_table_sql("clickhouse", table, cols, keys, mode, opts))
        elif not existing:
            raise ValueError(f"Target table {table} does not exist")
        elif mode == "upsert":
            self._require_replacing(table, keys)
        if existing and mode == "replace":
            stmts.append(f"TRUNCATE TABLE {qualified('clickhouse', table)}")
        if execute and stmts:
            client = self.c._client()
            try:
                for s in stmts:
                    client.command(s)
            finally:
                client.close()
        return stmts

    def _require_replacing(self, table: str, keys: list[str]) -> None:
        schema, name = split_name(table)
        rows = self._q("SELECT engine, sorting_key FROM system.tables WHERE database = %s AND name = %s", [schema or self._db(), name])
        engine, sorting = rows[0] if rows else ("", "")
        sort_cols = {c.strip().strip("`") for c in sorting.split(",") if c.strip()}
        if "Replacing" not in engine or not set(keys) <= sort_cols:
            raise ValueError(f"Upsert on ClickHouse needs a ReplacingMergeTree table whose ORDER BY contains ({', '.join(keys)}); "
                             f"{table} is {engine or 'unknown'} ordered by ({sorting})")

    def open_writer(self, table: str, cols: list[ColSpec], mode: str, keys: list[str]) -> Writer:
        schema, name = split_name(table)
        return ClickHouseWriter(self.c._client(read_timeout=600), schema or self._db(), name)


class ClickHouseWriter(Writer):
    def __init__(self, client, database: str, table: str):
        self.client = client
        self.database = database
        self.table = table

    def write(self, table: pa.Table) -> None:
        if table.num_rows:
            self.client.insert_arrow(self.table, table, database=self.database)

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


def _fill(sql: str, params: list[Any]) -> str:
    """Substitute %s placeholders with literals; %% becomes %."""
    return sql % tuple(lit(p) for p in params)
