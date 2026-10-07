import os
import sqlite3
from pathlib import Path

from .base import ColumnInfo, Connector, SplitCandidate, TableInfo, TableSchema
from .paths import PathNotAllowed, resolve_allowed

_TIME_HINTS = ("date", "time")
_NUMERIC_HINTS = ("int", "real", "numeric", "decimal", "double", "float")


class SQLiteConnector(Connector):
    type = "sqlite"
    label = "SQLite"
    abbr = "SL"
    kind = "file"
    description = "Embedded · local file"
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

    def _connect(self) -> sqlite3.Connection:
        p = self._path()
        read_only = bool(self.config.get("read_only"))
        if not p.exists():
            if read_only or not self.config.get("create_if_missing"):
                raise FileNotFoundError(f"{p} does not exist (enable 'create if missing' to make a new database)")
            p.parent.mkdir(parents=True, exist_ok=True)
        uri = f"file:{p}?mode={'ro' if read_only else 'rwc'}"
        return sqlite3.connect(uri, uri=True, timeout=self.timeout)

    def _test(self) -> tuple[str, str]:
        conn = self._connect()
        try:
            version = conn.execute("select sqlite_version()").fetchone()[0]
            n = conn.execute("select count(*) from sqlite_master where type='table' and name not like 'sqlite_%'").fetchone()[0]
            mode = "read-only" if self.config.get("read_only") else "read-write"
            return f"SQLite {version}", f"{self._path()} · {mode} · {n} tables"
        finally:
            conn.close()

    def list_tables(self) -> list[TableInfo]:
        conn = self._connect()
        try:
            names = [r[0] for r in conn.execute(
                "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
            size = os.path.getsize(self._path()) if self._path().exists() else None
            out = []
            for n in names:
                rows = conn.execute(f'select count(*) from "{n.replace(chr(34), chr(34) * 2)}"').fetchone()[0]
                out.append(TableInfo(name=f"main.{n}", est_rows=rows, exact=True))
            if out and size is not None and len(out) == 1:
                out[0].size_bytes = size
            return out
        finally:
            conn.close()

    def inspect(self, table: str) -> TableSchema:
        name = table.partition(".")[2] or table  # "main.orders" or just "orders"
        conn = self._connect()
        try:
            escaped = name.replace('"', '""')
            col_rows = conn.execute(f'PRAGMA table_info("{escaped}")').fetchall()  # cid, name, type, notnull, dflt, pk
            pk_cols = [r[1] for r in col_rows if r[5] > 0]
            leading: dict[str, bool] = {}
            for idx in conn.execute(f'PRAGMA index_list("{escaped}")').fetchall():  # seq, name, unique, origin, partial
                info = conn.execute(f'PRAGMA index_info("{idx[1]}")').fetchall()
                if info:
                    leading.setdefault(info[0][2], bool(idx[2]))
            sql_row = conn.execute("select sql from sqlite_master where type='table' and name=?", (name,)).fetchone()
            without_rowid = bool(sql_row and sql_row[0] and "without rowid" in sql_row[0].lower())
            rows = conn.execute(f'select count(*) from "{escaped}"').fetchone()[0]
        finally:
            conn.close()

        columns = [
            ColumnInfo(name=r[1], type=r[2] or "", nullable=not r[3], primary_key=r[5] > 0,
                       indexed=(r[5] > 0 or r[1] in leading))
            for r in col_rows
        ]
        col_meta = {c.name: c for c in columns}
        candidates: list[SplitCandidate] = []
        if len(pk_cols) == 1:
            pk_type = (col_meta[pk_cols[0]].type or "").lower()
            if without_rowid or pk_type == "integer":
                reason = "primary key (WITHOUT ROWID: physically clustered)" if without_rowid else "INTEGER PRIMARY KEY (alias for rowid)"
                mode = "time_range" if without_rowid and any(h in pk_type for h in _TIME_HINTS) else "id_range"
                candidates.append(SplitCandidate(column=pk_cols[0], mode=mode, verdict="recommended", reasons=[reason]))
        if not candidates and not without_rowid:
            candidates.append(SplitCandidate(column="rowid", mode="id_range", verdict="recommended",
                                              reasons=["implicit rowid: always indexed, present on every row"]))
        for col, unique in leading.items():
            if col in {c.column for c in candidates}:
                continue
            info = col_meta.get(col)
            t = (info.type or "").lower() if info else ""
            if any(h in t for h in _TIME_HINTS):
                mode = "time_range"
            elif any(h in t for h in _NUMERIC_HINTS):
                mode = "id_range"
            else:
                continue
            candidates.append(SplitCandidate(column=col, mode=mode, verdict="alternative",
                                              reasons=["unique index" if unique else "indexed"]))
        notes = [] if candidates else ["No usable key: consider a single stream."]
        return TableSchema(name=table, columns=columns, row_estimate=rows, exact=True, candidates=candidates, notes=notes)
