"""File inspect: columns, row estimate and a preview for a file under an allowed root.

Uses DuckDB for CSV/TSV, JSON/NDJSON and Parquet (all three are auto-detected natively), and
openpyxl for Excel, which DuckDB cannot read without an extra extension. This mirrors
Connector.inspect(): cheap-as-possible metadata plus a small sample, no full-table transforms.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from .connectors.base import ColumnInfo
from .connectors.paths import resolve_allowed

# Above this size, CSV/JSON row counts are not pre-computed (would mean a full file scan).
# Parquet row counts always come from the file's footer metadata, so they stay exact regardless.
_EXACT_COUNT_MAX_BYTES = 200 * 1024 * 1024

_FORMATS = {
    ".csv": "csv", ".tsv": "csv", ".txt": "csv",
    ".json": "json", ".ndjson": "json", ".jsonl": "json",
    ".parquet": "parquet",
    ".xlsx": "excel", ".xls": "excel",
}


class UnsupportedFormat(ValueError):
    pass


@dataclass
class FileInspection:
    path: str
    format: str
    columns: list[ColumnInfo]
    row_estimate: int | None = None
    exact: bool = False
    size_bytes: int | None = None
    preview: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect_format(path: Path) -> str:
    fmt = _FORMATS.get(path.suffix.lower())
    if fmt is None:
        raise UnsupportedFormat(f"Unsupported file extension: {path.suffix or '(none)'}")
    return fmt


def _duckdb_source(path: Path, fmt: str) -> str:
    p = str(path).replace("'", "''")
    if fmt == "csv":
        return f"read_csv_auto('{p}')"
    if fmt == "json":
        return f"read_json_auto('{p}')"
    return f"read_parquet('{p}')"


def _inspect_tabular(path: Path, fmt: str, size: int, sample_rows: int) -> FileInspection:
    con = duckdb.connect()
    try:
        src = _duckdb_source(path, fmt)
        rel = con.sql(f"select * from {src}")
        columns = [ColumnInfo(name=n, type=str(t)) for n, t in zip(rel.columns, rel.types)]
        preview = [dict(zip(rel.columns, r)) for r in con.sql(f"select * from {src} limit {sample_rows}").fetchall()]
        if fmt == "parquet" or size <= _EXACT_COUNT_MAX_BYTES:
            row_estimate, exact = con.sql(f"select count(*) from {src}").fetchone()[0], True
            notes: list[str] = []
        else:
            row_estimate, exact = None, False
            notes = [f"File is larger than {_EXACT_COUNT_MAX_BYTES // (1024 * 1024)} MB: row count is not pre-counted."]
    finally:
        con.close()
    return FileInspection(path=str(path), format=fmt, columns=columns, row_estimate=row_estimate, exact=exact,
                           size_bytes=size, preview=preview, notes=notes)


def _inspect_excel(path: Path, size: int, sample_rows: int) -> FileInspection:
    from openpyxl import load_workbook  # heavy import; only pulled in for xlsx/xls

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        total_rows = max((ws.max_row or 1) - 1, 0)  # minus header row
        rows_iter = ws.iter_rows(values_only=True)
        header = next(rows_iter, None)
        if header is None:
            return FileInspection(path=str(path), format="excel", columns=[], row_estimate=0, exact=True, size_bytes=size)
        header = [str(h) if h is not None else f"col_{i + 1}" for i, h in enumerate(header)]
        preview: list[dict[str, Any]] = []
        col_types: list[set[str]] = [set() for _ in header]
        for i, row in enumerate(rows_iter):
            if i >= sample_rows:
                break
            preview.append(dict(zip(header, row)))
            for j, v in enumerate(row[:len(header)]):
                if v is not None:
                    col_types[j].add(type(v).__name__)
        columns = [ColumnInfo(name=h, type=(t.pop() if len(t) == 1 else "mixed" if t else "unknown"))
                   for h, t in zip(header, col_types)]
        return FileInspection(path=str(path), format="excel", columns=columns, row_estimate=total_rows, exact=True,
                               size_bytes=size, preview=preview)
    finally:
        wb.close()


def inspect_file(path_str: str, sample_rows: int = 20) -> FileInspection:
    path = resolve_allowed(path_str)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"{path} does not exist")
    fmt = detect_format(path)
    size = path.stat().st_size
    if fmt == "excel":
        return _inspect_excel(path, size, sample_rows)
    return _inspect_tabular(path, fmt, size, sample_rows)
