"""MySQL / MariaDB adapter: unbuffered (streaming) reads, multi-row INSERT / ON DUPLICATE KEY UPDATE writes."""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pyarrow as pa
import pymysql
import pymysql.cursors
from pymysql.constants import FIELD_TYPE as FT

from ..arrowconv import dedupe_last, last_value, rows_to_table_lenient, table_rows
from ..ddl import TargetOptions, create_table_sql, qi, qualified, split_name
from ..model import Batch, ReadRequest, SplitSpec
from ..types import ColSpec, normalize
from .base import (
    Adapter, SqlDialect, Writer, build_count_sql, build_minmax_sql, build_probe_sql, build_read_sql,
)

_UTC = dt.timezone.utc
# session settings: UTC so TIMESTAMP columns behave the same everywhere; long write timeout so a slow consumer does not drop the stream
_INIT = "SET time_zone = '+00:00', net_write_timeout = 900, net_read_timeout = 900"


def to_bind(v: Any) -> Any:
    if isinstance(v, dt.datetime) and v.tzinfo is not None:
        return v.astimezone(_UTC).replace(tzinfo=None)
    return v


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

    # ---- schema ------------------------------------------------------------------
    def table_columns(self, table: str) -> list[ColSpec]:
        schema, name = self._schema(table)
        rows = self._rows(
            "select column_name, column_type, is_nullable from information_schema.columns "
            "where table_schema = COALESCE(%s, DATABASE()) and table_name = %s order by ordinal_position", [schema, name])
        return [ColSpec(n, normalize("mysql", t), nullable == "YES") for n, t, nullable in rows]

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
        if not existing and create:
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
        conn = self.c._connect(autocommit=False, init_command=_INIT)
        return MySQLWriter(conn, table, cols, keys if mode == "upsert" else [])


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
