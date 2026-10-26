"""File inspect: columns, row estimate and a preview for a file under an allowed root.

Uses DuckDB for CSV/TSV, JSON/NDJSON and Parquet (all three are auto-detected natively), and
openpyxl for Excel, which DuckDB cannot read without an extra extension. This mirrors
Connector.inspect(): cheap-as-possible metadata plus a small sample, no full-table transforms.

Compressed files: `.csv.gz` / `.tsv.gz` / `.json.gz` / `.ndjson.gz` / `.jsonl.gz` are read directly (DuckDB
decompresses gzip on the fly). A `.zip` must hold exactly one supported data file (`__MACOSX/` and dotfiles
are ignored); it is unpacked once into the file cache (app/filecache.py) and read from there.
"""
from __future__ import annotations

import posixpath
import shutil
import zipfile
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


_GZIP_OK = {"csv", "json"}  # gzip-compressed text; Parquet / Excel are compressed already


class UnsupportedFormat(ValueError):
    pass


@dataclass
class FileInspection:
    path: str
    format: str
    columns: list[ColumnInfo]
    compression: str | None = None  # gzip | zip
    row_estimate: int | None = None
    exact: bool = False
    size_bytes: int | None = None
    preview: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def split_compression(name: str) -> tuple[str, str | None]:
    """("orders.csv", "gzip") for "orders.csv.gz"; ("orders.zip", "zip") for a zip; (name, None) otherwise."""
    low = name.lower()
    if low.endswith(".gz"):
        return name[:-3], "gzip"
    if low.endswith(".zip"):
        return name, "zip"
    return name, None


def format_from_name(name: str) -> tuple[str | None, str | None]:
    """(format, compression) from a file name alone. A .zip's format is only known after looking inside (None)."""
    inner, comp = split_compression(name)
    if comp == "zip":
        return None, "zip"
    fmt = _FORMATS.get(posixpath.splitext(inner.lower())[1])
    if fmt is None:
        raise UnsupportedFormat(f"Unsupported file extension: {posixpath.splitext(name)[1] or '(none)'}")
    if comp == "gzip" and fmt not in _GZIP_OK:
        raise UnsupportedFormat(f"{name}: only CSV / TSV / JSON files can be read gzip-compressed "
                                "(Parquet and Excel files are compressed already)")
    return fmt, comp


def zip_member(path: Path) -> zipfile.ZipInfo:
    """The one supported data file inside a zip (folders, __MACOSX/ and dotfiles ignored)."""
    try:
        with zipfile.ZipFile(path) as zf:
            members = [m for m in zf.infolist() if not m.is_dir() and not m.filename.startswith("__MACOSX/")
                       and not posixpath.basename(m.filename).startswith(".")]
    except zipfile.BadZipFile as e:
        raise UnsupportedFormat(f"{path.name} is not a valid zip archive") from e
    data = []
    for m in members:
        try:
            fmt, comp = format_from_name(posixpath.basename(m.filename))
        except UnsupportedFormat:
            continue
        if comp is None and fmt:
            data.append(m)
    if len(data) != 1:
        found = ", ".join(posixpath.basename(m.filename) for m in members[:6]) or "nothing"
        raise UnsupportedFormat(f"{path.name} must contain exactly one CSV / TSV / JSON / Parquet / Excel file "
                                f"(found {len(data)} among: {found})")
    return data[0]


def detect_format(path: Path) -> str:
    fmt, comp = format_from_name(path.name)
    if comp == "zip":
        return format_from_name(posixpath.basename(zip_member(path).filename))[0] or ""
    return fmt or ""


def materialize(path: Path, unpack_dir: Path | None = None) -> Path:
    """The file to actually read: the path itself, or (for a .zip) its data file unpacked - into `unpack_dir` (a
    job's own folder, deleted with it) or else into the shared, size-capped file cache."""
    if split_compression(path.name)[1] != "zip":
        return path
    from . import filecache
    from .runtime import current_settings

    m = zip_member(path)
    st = path.stat()
    name = posixpath.basename(m.filename)
    cap = current_settings().remote_max_file_mb * 1024 * 1024
    if m.file_size > cap:
        raise UnsupportedFormat(f"{name} inside {path.name} is larger than the {cap // 1024 // 1024:,} MB limit")
    if unpack_dir is not None:
        dest = unpack_dir / f"{path.stem}__{filecache.safe_name(name)}"
        if dest.is_file() and dest.stat().st_size == m.file_size:
            return dest  # unpacked by an earlier run of the same job
        unpack_dir.mkdir(parents=True, exist_ok=True)
        filecache.ensure_space(unpack_dir, m.file_size, f"unpacking {name} from {path.name}")
    else:
        key = f"unzip:{path}:{st.st_size}:{st.st_mtime_ns}:{m.filename}"
        if hit := filecache.lookup(key, name):
            return hit
        dest = filecache.target(key, name)
        filecache.ensure_space(dest.parent, m.file_size, f"unpacking {name} from {path.name}")
    part = dest.with_name(dest.name + ".part")
    with zipfile.ZipFile(path) as zf, zf.open(m) as src, part.open("wb") as out:
        shutil.copyfileobj(src, out, 1024 * 1024)
    part.replace(dest)
    if unpack_dir is None:
        filecache.evict(keep=dest)
    return dest


def _duckdb_source(path: Path, fmt: str, opts: dict[str, Any] | None = None) -> str:
    """`opts`: the user's CSV overrides (header, skip_rows, delimiter); anything not given is auto-detected."""
    p = str(path).replace("'", "''")
    if fmt == "csv":
        o = opts or {}
        args = [f"'{p}'"]
        if o.get("header") is not None:
            args.append(f"header={'true' if o['header'] else 'false'}")
        if o.get("skip_rows"):
            args.append(f"skip={int(o['skip_rows'])}")
        if o.get("delimiter"):
            args.append("delim='" + str(o["delimiter"]).replace("'", "''") + "'")
        return f"read_csv_auto({', '.join(args)})"
    if fmt == "json":
        return f"read_json_auto('{p}')"
    return f"read_parquet('{p}')"


def _inspect_tabular(path: Path, fmt: str, size: int, sample_rows: int, gz: bool = False,
                     opts: dict[str, Any] | None = None) -> FileInspection:
    con = duckdb.connect()
    try:
        src = _duckdb_source(path, fmt, opts)
        rel = con.sql(f"select * from {src}")
        columns = [ColumnInfo(name=n, type=str(t)) for n, t in zip(rel.columns, rel.types)]
        preview = [dict(zip(rel.columns, r)) for r in con.sql(f"select * from {src} limit {sample_rows}").fetchall()]
        limit = _EXACT_COUNT_MAX_BYTES // (5 if gz else 1)  # gzip text unpacks to roughly 5x its size
        if fmt == "parquet" or size <= limit:
            row_estimate, exact = con.sql(f"select count(*) from {src}").fetchone()[0], True
            notes: list[str] = []
        else:
            row_estimate, exact = None, False
            notes = [f"File is larger than {limit // (1024 * 1024)} MB{' compressed' if gz else ''}: "
                     "row count is not pre-counted."]
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


def inspect_file(path_str: str, sample_rows: int = 20, opts: dict[str, Any] | None = None) -> FileInspection:
    path = resolve_allowed(path_str)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"{path} does not exist")
    return inspect_local(path, sample_rows, opts=opts)


def inspect_local(path: Path, sample_rows: int = 20, fmt: str | None = None,
                  opts: dict[str, Any] | None = None) -> FileInspection:
    """Inspect a file already known to be safe to read (an allowed path, or a cached / downloaded copy)."""
    _, comp = split_compression(path.name)
    fmt = fmt or detect_format(path)
    size = path.stat().st_size
    data = materialize(path)
    if fmt == "excel":
        res = _inspect_excel(data, size, sample_rows)
    else:
        res = _inspect_tabular(data, fmt, size, sample_rows, gz=comp == "gzip", opts=opts)
    res.path, res.compression = str(path), comp
    if comp == "zip":
        res.notes.insert(0, f"Read from {data.name} inside the zip archive.")
    return res
