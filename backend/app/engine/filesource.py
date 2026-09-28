"""Files as a source: CSV/TSV, JSON/NDJSON, Parquet (DuckDB) and Excel (openpyxl).

Implements the read side of the Adapter interface, so the runner treats a file like a table. CSV columns are
emitted as text and cast by the transform step, which is what gives row-level "cannot convert" rejects.
Rows the CSV parser itself cannot make sense of (wrong column count, broken quoting) come back as rejects too.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from ..connectors.paths import resolve_allowed
from ..files import detect_format
from .arrowconv import conform, rows_to_table_lenient
from .ddl import qi
from .model import Batch, ReadRequest, SplitSpec
from .types import ColSpec, normalize

_EXACT_COUNT_MAX_BYTES = 200 * 1024 * 1024


@dataclass
class FileSpec:
    path: str
    format: str | None = None  # csv | json | parquet | excel; detected from the extension when empty
    delimiter: str | None = None  # csv; auto-detected when empty
    header: bool = True
    encoding: str = "utf-8"
    sheet: str | None = None  # excel; first sheet when empty
    skip_rows: int = 0
    null_values: list[str] = field(default_factory=lambda: [""])  # csv strings that mean NULL


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


class FileAdapter:
    """Source-only adapter over one file."""

    dialect = "file"

    def __init__(self, spec: FileSpec):
        self.spec = spec
        self.path: Path = resolve_allowed(spec.path)
        if not self.path.exists() or not self.path.is_file():
            raise FileNotFoundError(f"{self.path} does not exist")
        self.format = spec.format or detect_format(self.path)
        if self.path.suffix.lower() == ".xls":
            raise ValueError("Legacy .xls files are not supported: save the sheet as .xlsx or .csv")
        self.notes: list[str] = []
        self.max_writers = 0

    # ---- DuckDB relation ---------------------------------------------------------
    def _relation(self, all_text: bool = True) -> str:
        p, s = _q(str(self.path)), self.spec
        if self.format == "csv":
            opts = [f"header={'true' if s.header else 'false'}", f"encoding={_q(s.encoding)}", "ignore_errors=true",
                    "store_rejects=true", f"nullstr=[{', '.join(_q(v) for v in s.null_values)}]", "null_padding=false"]
            if all_text:
                opts.append("all_varchar=true")
            delim = s.delimiter or ("\t" if self.path.suffix.lower() == ".tsv" else None)
            if delim:
                opts.append(f"delim={_q(delim)}")
            if s.skip_rows:
                opts.append(f"skip={int(s.skip_rows)}")
            return f"read_csv({p}, {', '.join(opts)})"
        if self.format == "json":
            return f"read_json_auto({p})"
        return f"read_parquet({p})"

    # ---- columns -----------------------------------------------------------------
    def read_columns(self) -> list[ColSpec]:
        """Columns exactly as batches will carry them (CSV: all text)."""
        if self.format == "excel":
            return self._excel_columns()[0]
        con = duckdb.connect()
        try:
            rel = con.sql(f"SELECT * FROM {self._relation()} LIMIT 0")
            return [ColSpec(n, normalize("duckdb", str(t)), True) for n, t in zip(rel.columns, rel.types)]
        finally:
            con.close()

    def suggest_columns(self) -> list[ColSpec]:
        """Best-guess typed columns, used as the wizard's default target types."""
        if self.format == "excel":
            return self._excel_columns()[0]
        con = duckdb.connect()
        try:
            opts = self._relation(all_text=False)
            rel = con.sql(f"SELECT * FROM {opts} LIMIT 0")
            return [ColSpec(n, normalize("duckdb", str(t)), True) for n, t in zip(rel.columns, rel.types)]
        finally:
            con.close()

    def _excel_columns(self) -> tuple[list[ColSpec], list[str]]:
        from openpyxl import load_workbook

        wb = load_workbook(self.path, read_only=True, data_only=True)
        try:
            ws = wb[self.spec.sheet] if self.spec.sheet else wb.worksheets[0]
            rows = ws.iter_rows(values_only=True)
            for _ in range(self.spec.skip_rows):
                next(rows, None)
            first = next(rows, None)
            if first is None:
                return [], []
            width = len(first)
            names: list[str] = []
            for i, h in enumerate(first if self.spec.header else [None] * width):
                base = str(h).strip() if h not in (None, "") else f"col_{i + 1}"
                name, k = base, 1
                while name in names:
                    k += 1
                    name = f"{base}_{k}"
                names.append(name)
            seen: list[set[str]] = [set() for _ in names]
            sample = [first] if not self.spec.header else []
            for i, row in enumerate(rows):
                if i >= 1000:
                    break
                sample.append(row)
            for row in sample:
                for j, v in enumerate(row[:width]):
                    if v is not None:
                        seen[j].add(type(v).__name__)
            def logical(ts: set[str]) -> str:
                if not ts:
                    return "VARCHAR"
                if ts <= {"int"}:
                    return "BIGINT"
                if ts <= {"int", "float"}:
                    return "DOUBLE"
                if ts == {"bool"}:
                    return "BOOLEAN"
                if ts == {"datetime"}:
                    return "TIMESTAMP"
                if ts == {"date"}:
                    return "DATE"
                return "VARCHAR"
            return [ColSpec(n, logical(t), True) for n, t in zip(names, seen)], names
        finally:
            wb.close()

    # ---- Adapter-like interface --------------------------------------------------
    def estimate_rows(self, req: ReadRequest | None = None, exact: bool = False) -> tuple[int | None, bool]:
        size = self.path.stat().st_size
        if self.format == "excel":
            from openpyxl import load_workbook

            wb = load_workbook(self.path, read_only=True, data_only=True)
            try:
                ws = wb[self.spec.sheet] if self.spec.sheet else wb.worksheets[0]
                return max((ws.max_row or 1) - (1 if self.spec.header else 0) - self.spec.skip_rows, 0), True
            finally:
                wb.close()
        con = duckdb.connect()
        try:
            if self.format == "parquet" or exact or size <= _EXACT_COUNT_MAX_BYTES:
                return int(con.sql(f"SELECT count(*) FROM {self._relation()}").fetchone()[0]), True
            sample = con.sql(f"SELECT count(*) FROM (SELECT * FROM {self._relation()} LIMIT 20000)").fetchone()[0]
            with self.path.open("rb") as f:
                head = f.read(min(size, 8 * 1024 * 1024))
            lines = max(head.count(b"\n"), 1)
            return int(size / (len(head) / lines)) if sample else 0, False
        finally:
            con.close()

    def key_range(self, req: ReadRequest) -> None:
        return None

    def plan_splits(self, req: ReadRequest, n: int) -> list[SplitSpec]:
        return [SplitSpec(0, self.path.name, "full")]  # one reader per file; DuckDB parallelises inside it

    def read_batches(self, req: ReadRequest, split: SplitSpec, batch_rows: int) -> Iterator[Batch]:
        if self.format == "excel":
            yield from self._read_excel(req, batch_rows)
            return
        cols = ", ".join(qi("duckdb", c.name) for c in req.columns)
        con = duckdb.connect()
        try:
            res = con.execute(f"SELECT {cols} FROM {self._relation()}")
            reader = (res.to_arrow_reader if hasattr(res, "to_arrow_reader") else res.fetch_record_batch)(batch_rows)
            for rb in reader:
                if rb.num_rows:
                    yield Batch(conform(pa.Table.from_batches([rb]), req.columns))
            if self.format == "csv":
                yield Batch(pa.table({c.name: pa.array([], type=pa.string()) for c in req.columns}), None, self._csv_rejects(con, req))
        finally:
            con.close()

    def _csv_rejects(self, con: duckdb.DuckDBPyConnection, req: ReadRequest) -> list[tuple[str, dict[str, Any]]]:
        try:
            rows = con.execute("SELECT line, column_name, error_type, error_message, csv_line FROM reject_errors ORDER BY line").fetchall()
        except duckdb.Error:
            return []  # no rejects table: nothing was rejected
        by_line: dict[int, tuple[list[str], str]] = {}
        for ln, col, etype, _msg, raw in rows:  # one reject per bad line, even when several columns are affected
            reasons, _ = by_line.setdefault(ln, ([], (raw or "").strip("\r\n")))
            label = f"{etype} ({col})" if col else str(etype)
            if label not in reasons:
                reasons.append(label)
        return [(f"line {ln}: " + "; ".join(r), {"line": ln, "raw": raw}) for ln, (r, raw) in sorted(by_line.items())]

    def _read_excel(self, req: ReadRequest, batch_rows: int) -> Iterator[Batch]:
        from openpyxl import load_workbook

        cols, names = self._excel_columns()
        idx = {n: i for i, n in enumerate(names)}
        wanted = [idx[c.name] for c in req.columns]
        wb = load_workbook(self.path, read_only=True, data_only=True)
        try:
            ws = wb[self.spec.sheet] if self.spec.sheet else wb.worksheets[0]
            rows = ws.iter_rows(values_only=True)
            for _ in range(self.spec.skip_rows + (1 if self.spec.header else 0)):
                next(rows, None)
            buf: list[tuple] = []
            for row in rows:
                if all(v is None for v in row):
                    continue
                padded = tuple(row) + (None,) * (len(names) - len(row))
                buf.append(tuple(padded[i] for i in wanted))
                if len(buf) >= batch_rows:
                    table, rejects = rows_to_table_lenient(buf, req.columns)
                    yield Batch(table, None, rejects)
                    buf = []
            if buf:
                table, rejects = rows_to_table_lenient(buf, req.columns)
                yield Batch(table, None, rejects)
        finally:
            wb.close()
