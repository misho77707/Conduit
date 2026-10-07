"""DuckDB adapter: Arrow in and out, so batches skip the row-by-row Python path entirely."""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pyarrow as pa

from ..arrowconv import conform, dedupe_last, last_value
from ..ddl import TargetOptions, create_table_sql, qi, qualified, split_name
from ..model import Batch, ReadRequest, SplitSpec
from ..types import ColSpec, normalize
from .base import Adapter, SqlDialect, Writer, build_count_sql, build_minmax_sql, build_read_sql


class DuckDBAdapter(Adapter):
    dialect = "duckdb"
    sql = SqlDialect("duckdb", "?", False)
    max_writers = 1

    def _rows(self, sql: str, params: list[Any] | None = None) -> list[tuple]:
        conn = self.c._connect()
        try:
            return conn.execute(sql, params or []).fetchall()
        finally:
            conn.close()

    # ---- schema ------------------------------------------------------------------
    def table_columns(self, table: str) -> list[ColSpec]:
        schema, name = split_name(table)
        rows = self._rows(
            "select column_name, data_type, is_nullable, column_default from information_schema.columns "
            "where table_schema = ? and table_name = ? order by ordinal_position", [schema or "main", name])
        return [ColSpec(n, normalize("duckdb", t), nullable == "YES", default=d) for n, t, nullable, d in rows]

    def query_columns(self, sql: str) -> list[ColSpec]:
        rows = self._rows(f"DESCRIBE SELECT * FROM ({sql.rstrip().rstrip(';')}) AS conduit_q")
        return [ColSpec(r[0], normalize("duckdb", r[1]), True) for r in rows]

    def is_unique(self, table: str, column: str) -> bool:
        schema, name = split_name(table)
        rows = self._rows(
            "select constraint_column_names from duckdb_constraints() "
            "where schema_name = ? and table_name = ? and constraint_type in ('PRIMARY KEY', 'UNIQUE')", [schema or "main", name])
        return any(list(r[0]) == [column] for r in rows)

    # ---- source ------------------------------------------------------------------
    def estimate_rows(self, req: ReadRequest, exact: bool = False) -> tuple[int | None, bool]:
        sql, params = build_count_sql(self.sql, req)
        return int(self._rows(sql, params)[0][0]), True

    def key_range(self, req: ReadRequest) -> tuple[Any, Any] | None:
        sql, params = build_minmax_sql(self.sql, req)
        mn, mx, cnt = self._rows(sql, params)[0]
        return None if mn is None or not cnt else (mn, mx)

    def read_batches(self, req: ReadRequest, split: SplitSpec, batch_rows: int) -> Iterator[Batch]:
        sql, params = build_read_sql(self.sql, req, split)
        conn = self.c._connect()
        try:
            res = conn.execute(sql, params)
            reader = (res.to_arrow_reader if hasattr(res, "to_arrow_reader") else res.fetch_record_batch)(batch_rows)
            for rb in reader:
                if rb.num_rows == 0:
                    continue
                table = conform(pa.Table.from_batches([rb]), req.columns)
                yield Batch(table, last_value(table, split.column) if split.kind == "range" and split.column else None)
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
            if schema and schema != "main":
                stmts.append(f"CREATE SCHEMA IF NOT EXISTS {qi('duckdb', schema)}")
            stmts.append(create_table_sql("duckdb", table, cols, keys, mode, opts))
        elif not existing:
            raise ValueError(f"Target table {table} does not exist")
        elif mode == "upsert":
            self._require_unique(table, keys)
        if existing and mode == "replace":
            stmts.append(f"DELETE FROM {qualified('duckdb', table)}")
        if execute and stmts:
            conn = self.c._connect()
            try:
                for s in stmts:
                    conn.execute(s)
            finally:
                conn.close()
        return stmts

    def _require_unique(self, table: str, keys: list[str]) -> None:
        schema, name = split_name(table)
        rows = self._rows(
            "select constraint_column_names from duckdb_constraints() "
            "where schema_name = ? and table_name = ? and constraint_type in ('PRIMARY KEY', 'UNIQUE')",
            [schema or "main", name])
        if not any(set(r[0]) == set(keys) for r in rows):
            raise ValueError(f"Upsert needs a PRIMARY KEY or UNIQUE constraint on ({', '.join(keys)}) in {table}")

    def open_writer(self, table: str, cols: list[ColSpec], mode: str, keys: list[str]) -> Writer:
        return DuckDBWriter(self.c._connect(), table, cols, keys if mode == "upsert" else [])


class DuckDBWriter(Writer):
    def __init__(self, conn, table: str, cols: list[ColSpec], keys: list[str]):
        self.conn = conn
        self.keys = keys
        self.view = "conduit_batch_" + uuid.uuid4().hex[:8]
        names = [c.name for c in cols]
        cl = ", ".join(qi("duckdb", n) for n in names)
        sql = f"INSERT INTO {qualified('duckdb', table)} ({cl}) SELECT {cl} FROM {self.view}"
        if keys:
            nonkey = [n for n in names if n not in keys]
            conflict = ", ".join(qi("duckdb", k) for k in keys)
            sql += (f" ON CONFLICT ({conflict}) DO UPDATE SET "
                    + ", ".join(f"{qi('duckdb', n)} = EXCLUDED.{qi('duckdb', n)}" for n in nonkey)) if nonkey \
                else f" ON CONFLICT ({conflict}) DO NOTHING"
        self.sql = sql

    def write(self, table: pa.Table) -> None:
        if table.num_rows == 0:
            return
        table = dedupe_last(table, self.keys)
        self.conn.register(self.view, table)
        try:
            self.conn.begin()
            try:
                self.conn.execute(self.sql)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        finally:
            self.conn.unregister(self.view)

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
