import threading
from pathlib import Path
from typing import Any

import duckdb

from .base import ColumnInfo, Connector, SplitCandidate, TableInfo, TableSchema
from .paths import PathNotAllowed, resolve_allowed

_TIME_TYPES = ("TIMESTAMP", "DATE")
_NUMERIC_TYPES = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "DECIMAL", "DOUBLE", "FLOAT", "UINTEGER", "UBIGINT")


_lock = threading.Lock()
_shared: dict[tuple[str, bool], list[Any]] = {}  # (file, read_only) -> [instance, open handles]


class _Handle:
    """A cursor on one process-wide DuckDB instance per file.

    The instance is opened by the first handle and closed by the last, both under one lock. Without it, parallel splits
    that connect while another thread is opening or closing the same file fail with "Unique file handle conflict".
    Everything else is forwarded to the underlying cursor.
    """

    def __init__(self, key: tuple[str, bool], cur: Any) -> None:
        self._key, self._cur, self._closed = key, cur, False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cur, name)

    def close(self) -> None:
        with _lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._cur.close()
            finally:
                ent = _shared[self._key]
                ent[1] -= 1
                if ent[1] == 0:
                    del _shared[self._key]
                    ent[0].close()


def open_shared(path: str, read_only: bool) -> _Handle:
    key = (path, read_only)
    with _lock:
        ent = _shared.get(key)
        if ent is None:
            ent = _shared[key] = [duckdb.connect(path, read_only=read_only), 0]
        try:
            cur = ent[0].cursor()
        except Exception:
            if ent[1] == 0:
                del _shared[key]
                ent[0].close()
            raise
        ent[1] += 1
        return _Handle(key, cur)


class DuckDBConnector(Connector):
    type = "duckdb"
    label = "DuckDB"
    abbr = "DK"
    kind = "file"
    description = "Embedded OLAP · local file"
    secret_fields = ()

    @classmethod
    def validate(cls, config, secrets):
        try:
            resolve_allowed(str(config.get("path", "")))
        except PathNotAllowed as e:
            return [str(e)]
        return []

    def display_host(self) -> str:
        return str(self.config.get("path", ""))

    def _path(self) -> Path:
        return resolve_allowed(str(self.config.get("path", "")))

    def _connect(self) -> _Handle:
        p = self._path()
        read_only = bool(self.config.get("read_only"))
        if not p.exists():
            if read_only or not self.config.get("create_if_missing"):
                raise FileNotFoundError(f"{p} does not exist (enable 'create if missing' to make a new database)")
            p.parent.mkdir(parents=True, exist_ok=True)
        return open_shared(str(p), read_only)

    def _test(self) -> tuple[str, str]:
        conn = self._connect()
        try:
            version = conn.execute("select version()").fetchone()[0]
            n = conn.execute("select count(*) from duckdb_tables()").fetchone()[0]
            mode = "read-only" if self.config.get("read_only") else "read-write"
            return f"DuckDB {version.lstrip('v')}", f"{self._path()} · {mode} · {n} tables"
        finally:
            conn.close()

    def list_tables(self) -> list[TableInfo]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "select schema_name, table_name, estimated_size from duckdb_tables() order by 1, 2").fetchall()
            return [TableInfo(name=f"{s}.{t}", est_rows=int(r) if r is not None else None) for s, t, r in rows]
        finally:
            conn.close()

    def inspect(self, table: str) -> TableSchema:
        schema, _, name = table.partition(".")
        conn = self._connect()
        try:
            col_rows = conn.execute(
                "select column_name, data_type, is_nullable from duckdb_columns() "
                "where schema_name = ? and table_name = ? order by column_index", (schema, name),
            ).fetchall()
            pk_rows = conn.execute(
                "select constraint_column_names from duckdb_constraints() "
                "where schema_name = ? and table_name = ? and constraint_type = 'PRIMARY KEY'", (schema, name),
            ).fetchall()
            size_row = conn.execute(
                "select estimated_size from duckdb_tables() where schema_name = ? and table_name = ?", (schema, name),
            ).fetchone()
        finally:
            conn.close()

        pk_cols = list(pk_rows[0][0]) if pk_rows and pk_rows[0][0] else []
        columns = [ColumnInfo(name=n, type=t, nullable=bool(is_null), primary_key=n in pk_cols, indexed=n in pk_cols)
                   for n, t, is_null in col_rows]
        candidates: list[SplitCandidate] = []
        if len(pk_cols) == 1:
            pk_type = next((c.type.upper() for c in columns if c.name == pk_cols[0]), "")
            mode = "time_range" if pk_type.startswith(_TIME_TYPES) else "id_range" if pk_type.startswith(_NUMERIC_TYPES) else None
            if mode:
                candidates.append(SplitCandidate(
                    column=pk_cols[0], mode=mode, verdict="alternative", reasons=["primary key"],
                    warnings=["DuckDB serializes writes regardless of split count"],
                ))
        notes = [] if candidates else [
            "No numeric/timestamp primary key: DuckDB parallelizes reads via row groups automatically, "
            "so a single stream is fine (writes are serialized either way)."
        ]
        return TableSchema(
            name=table, columns=columns,
            row_estimate=int(size_row[0]) if size_row and size_row[0] is not None else None,
            candidates=candidates, notes=notes,
        )
