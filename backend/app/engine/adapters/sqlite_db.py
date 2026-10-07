"""SQLite adapter (embedded, one writer at a time). Dates and timestamps are stored as ISO-8601 text in UTC."""
from __future__ import annotations

import datetime as dt
import decimal
from collections.abc import Iterator
from typing import Any

import pyarrow as pa

from ..arrowconv import coerce, dedupe_last, last_value, rows_to_table_lenient, table_rows
from ..ddl import TargetOptions, create_table_sql, qi, qualified, split_name
from ..model import Batch, ReadRequest, SplitSpec
from ..types import ColSpec, clean, normalize, to_arrow
from .base import Adapter, SqlDialect, Writer, build_count_sql, build_minmax_sql, build_read_sql

_UTC = dt.timezone.utc


def to_bind(v: Any) -> Any:
    """Python value -> something sqlite3 can bind (no deprecated default adapters)."""
    if isinstance(v, dt.datetime):
        if v.tzinfo is not None:
            v = v.astimezone(_UTC).replace(tzinfo=None)
        return v.isoformat(" ")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, memoryview):
        return bytes(v)
    return v


class SQLiteAdapter(Adapter):
    dialect = "sqlite"
    sql = SqlDialect("sqlite", "?", False)
    max_writers = 1

    def _rows(self, sql: str, params: list[Any] | tuple = ()) -> list[tuple]:
        conn = self.c._connect()
        try:
            return conn.execute(sql, [to_bind(p) for p in params]).fetchall()
        finally:
            conn.close()

    # ---- schema ------------------------------------------------------------------
    def table_columns(self, table: str) -> list[ColSpec]:
        _, name = split_name(table)
        rows = self._rows(f"PRAGMA table_info({qi('sqlite', name)})")
        return [ColSpec(r[1], normalize("sqlite", r[2] or ""), not (r[3] or r[5]), default=r[4]) for r in rows]

    def query_columns(self, sql: str) -> list[ColSpec]:
        conn = self.c._connect()
        try:
            cur = conn.execute(f"SELECT * FROM ({sql.rstrip().rstrip(';')}) AS conduit_q LIMIT 1")
            row = cur.fetchone()
            out = []
            for i, d in enumerate(cur.description or []):
                v = row[i] if row else None
                logical = ("BIGINT" if isinstance(v, int) else "DOUBLE" if isinstance(v, float)
                           else "BLOB" if isinstance(v, bytes) else "TEXT")
                out.append(ColSpec(d[0], logical, True))
            return out
        finally:
            conn.close()

    def is_unique(self, table: str, column: str) -> bool:
        _, name = split_name(table)
        info = self._rows(f"PRAGMA table_info({qi('sqlite', name)})")
        pk = [r[1] for r in info if r[5]]
        if pk == [column]:
            return True
        for idx in self._rows(f"PRAGMA index_list({qi('sqlite', name)})"):
            if idx[2] and [r[2] for r in self._rows(f"PRAGMA index_info({qi('sqlite', idx[1])})")] == [column]:
                return True
        return False

    # ---- source ------------------------------------------------------------------
    def estimate_rows(self, req: ReadRequest, exact: bool = False) -> tuple[int | None, bool]:
        sql, params = build_count_sql(self.sql, req)
        return int(self._rows(sql, params)[0][0]), True  # no catalog estimate in SQLite; COUNT(*) is cheap enough

    def key_range(self, req: ReadRequest) -> tuple[Any, Any] | None:
        sql, params = build_minmax_sql(self.sql, req)
        mn, mx, cnt = self._rows(sql, params)[0]
        if mn is None or not cnt:
            return None
        if self.is_time(req.split_col_logical):
            t = clean(req.split_col_logical or "")
            conv = (lambda s: coerce(s, to_arrow("DATE"))) if t == "DATE" else (lambda s: coerce(s, to_arrow("TIMESTAMP")))
            return conv(mn), conv(mx)
        return mn, mx

    def read_batches(self, req: ReadRequest, split: SplitSpec, batch_rows: int) -> Iterator[Batch]:
        sql, params = build_read_sql(self.sql, req, split)
        conn = self.c._connect()
        try:
            conn.execute("PRAGMA query_only = ON")
            cur = conn.execute(sql, [to_bind(p) for p in params])
            while True:
                rows = cur.fetchmany(batch_rows)
                if not rows:
                    break
                table, rejects = rows_to_table_lenient(rows, req.columns)
                yield Batch(table, last_value(table, split.column) if split.kind == "range" and split.column else None, rejects)
        finally:
            conn.close()

    # ---- target ------------------------------------------------------------------
    def prepare_target(self, table: str, cols: list[ColSpec], mode: str, keys: list[str], opts: TargetOptions,
                       create: bool, execute: bool) -> list[str]:
        existing = self.table_columns(table)
        stmts: list[str] = []
        if not existing and create and opts.ddl:
            stmts += opts.ddl
        elif not existing and create:
            stmts.append(create_table_sql("sqlite", table, cols, keys, mode, opts))
        elif not existing:
            raise ValueError(f"Target table {table} does not exist")
        elif mode == "upsert":
            self._require_unique(table, keys)
        if existing and mode == "replace":
            stmts.append(f"DELETE FROM {qualified('sqlite', table)}")
        if execute and stmts:
            conn = self.c._connect()
            try:
                with conn:
                    for s in stmts:
                        conn.execute(s)
            finally:
                conn.close()
        return stmts

    def _require_unique(self, table: str, keys: list[str]) -> None:
        _, name = split_name(table)
        pk = [r[1] for r in sorted(self._rows(f"PRAGMA table_info({qi('sqlite', name)})"), key=lambda r: r[5]) if r[5]]
        sets = [set(pk)] if pk else []
        for idx in self._rows(f"PRAGMA index_list({qi('sqlite', name)})"):
            if idx[2]:  # unique
                sets.append({r[2] for r in self._rows(f"PRAGMA index_info({qi('sqlite', idx[1])})")})
        if not any(s == set(keys) for s in sets):
            raise ValueError(f"Upsert needs a PRIMARY KEY or UNIQUE index on ({', '.join(keys)}) in {table}")

    def open_writer(self, table: str, cols: list[ColSpec], mode: str, keys: list[str]) -> Writer:
        return SQLiteWriter(self.c._connect(), table, cols, keys if mode == "upsert" else [])


class SQLiteWriter(Writer):
    def __init__(self, conn, table: str, cols: list[ColSpec], keys: list[str]):
        self.conn = conn
        names = [c.name for c in cols]
        marks = ", ".join("?" for _ in names)
        sql = f"INSERT INTO {qualified('sqlite', table)} ({', '.join(qi('sqlite', n) for n in names)}) VALUES ({marks})"
        self.keys = keys
        if keys:
            nonkey = [n for n in names if n not in keys]
            conflict = ", ".join(qi("sqlite", k) for k in keys)
            sql += (f" ON CONFLICT ({conflict}) DO UPDATE SET "
                    + ", ".join(f"{qi('sqlite', n)} = excluded.{qi('sqlite', n)}" for n in nonkey)) if nonkey \
                else f" ON CONFLICT ({conflict}) DO NOTHING"
        self.sql = sql

    def write(self, table: pa.Table) -> None:
        if table.num_rows == 0:
            return
        table = dedupe_last(table, self.keys)
        rows = [tuple(to_bind(v) for v in r) for r in table_rows(table)]
        with self.conn:  # one transaction per batch
            self.conn.executemany(self.sql, rows)

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
