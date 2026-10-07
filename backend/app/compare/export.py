"""Reading a finished comparison's result sets: paged samples for the UI and file exports."""
from __future__ import annotations

import datetime as dt
import decimal
import math
import re
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any

import duckdb

from ..engine.arrowconv import _jsonable
from ..engine.runner import JobError
from .engine import SETS, folder, results_path

FORMATS = {"csv": ("csv", "text/csv"), "xlsx": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
           "parquet": ("parquet", "application/vnd.apache.parquet"), "json": ("ndjson", "application/x-ndjson")}
SET_LABELS = {"missing": "missing in target", "extra": "extra in target", "mismatched": "value differences",
              "dup_source": "duplicates in source", "dup_target": "duplicates in target"}
EXCEL_MAX_ROWS = 1_048_575  # one sheet holds 1,048,576 rows including the header


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _open(cid: uuid.UUID | str) -> duckdb.DuckDBPyConnection:
    p = results_path(cid)
    if not p.exists():
        raise FileNotFoundError("The results of this comparison are no longer available")
    return duckdb.connect(str(p), read_only=True)


def select_list(summary: dict[str, Any], set_name: str, columns: str | list[str]) -> list[str]:
    """Quoted column names of one result set for `columns` = "all", "keys" or a list of compared column names.

    Key columns always come first (so every exported row can be found again); value differences carry the
    source / target value of each chosen column plus the list of columns that differ.
    """
    names = [c["name"] for c in summary["columns"]]
    keys = list(summary.get("keys") or [])
    if columns == "all":
        chosen = names
    elif columns == "keys":
        chosen = keys or names
    else:
        unknown = [c for c in columns if c not in names]
        if unknown:
            raise JobError(f"Unknown column(s): {', '.join(unknown)}")
        chosen = keys + [c for c in columns if c not in keys]
    if set_name == "mismatched":
        vals = [c for c in chosen if c not in keys]
        return [_q(k) for k in keys] + [_q(f"{c}__{side}") for c in vals for side in ("source", "target")] + ["_diff_columns"]
    out = [_q(c) for c in (chosen if not keys else keys + [c for c in chosen if c not in keys])]
    return out + (["_count"] if set_name.startswith("dup_") else [])


def _check_set(summary: dict[str, Any], set_name: str) -> None:
    if set_name not in SETS:
        raise JobError(f"Unknown result set {set_name!r} (one of: {', '.join(SETS)})")
    if set_name not in (summary.get("sets") or {}):
        raise JobError(f"This comparison has no {SET_LABELS[set_name]} set"
                       + (" (rows were not matched by key)" if set_name == "mismatched" else ""))


def _clean(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else v
    if isinstance(v, decimal.Decimal) and v == v.to_integral_value() and abs(v) < 2**53:
        return int(v)  # whole numbers stay numbers; other decimals go out as exact strings
    return _jsonable(v)


def rows(cid: uuid.UUID | str, summary: dict[str, Any], set_name: str, offset: int, limit: int) -> dict[str, Any]:
    _check_set(summary, set_name)
    con = _open(cid)
    try:
        cols = select_list(summary, set_name, "all")
        cur = con.execute(f"SELECT {', '.join(cols)} FROM {set_name} LIMIT {int(limit)} OFFSET {int(offset)}")
        names = [d[0] for d in cur.description]
        data = [{n: _clean(v) for n, v in zip(names, r)} for r in cur.fetchall()]
        return {"set": set_name, "columns": names, "rows": data, "total": summary["sets"][set_name],
                "offset": offset, "limit": limit}
    finally:
        con.close()


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")[:60] or "comparison"


def export(cid: uuid.UUID | str, name: str, summary: dict[str, Any], sets: list[str], columns: str | list[str],
           fmt: str, sample: bool, sample_rows: int) -> tuple[Path, str, str]:
    """Write the export to a temp folder; returns (file, download name, media type). The caller deletes the folder."""
    if fmt not in FORMATS:
        raise JobError(f"Unknown format {fmt!r} (one of: {', '.join(FORMATS)})")
    if not sets:
        raise JobError("Choose at least one result set to export")
    for s in sets:
        _check_set(summary, s)
    ext, media = FORMATS[fmt]
    base = _slug(name)
    tmp = folder(cid) / "exports" / uuid.uuid4().hex
    tmp.mkdir(parents=True, exist_ok=True)
    limit = f" LIMIT {int(sample_rows)}" if sample else ""
    con = _open(cid)
    try:
        if fmt == "xlsx":
            out = tmp / f"{base}.xlsx"
            _excel(con, summary, sets, columns, limit, out)
            return out, out.name, media
        files = []
        for s in sets:
            f = tmp / f"{base}-{s}.{ext}"
            query = f"SELECT {', '.join(select_list(summary, s, columns))} FROM {s}{limit}"
            opts = {"csv": "FORMAT csv, HEADER true", "parquet": "FORMAT parquet", "json": "FORMAT json"}[fmt]
            con.execute(f"COPY ({query}) TO '{str(f).replace(chr(39), chr(39) * 2)}' ({opts})")
            files.append(f)
    finally:
        con.close()
    if len(files) == 1:
        return files[0], files[0].name, media
    z = tmp / f"{base}.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)
    return z, z.name, "application/zip"


def _excel(con, summary: dict[str, Any], sets: list[str], columns, limit: str, out: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook(write_only=True)
    for s in sets:
        ws = wb.create_sheet(SET_LABELS[s][:31])
        cur = con.execute(f"SELECT {', '.join(select_list(summary, s, columns))} FROM {s}{limit}")
        ws.append([d[0] for d in cur.description])
        written = 0
        while written < EXCEL_MAX_ROWS:
            chunk = cur.fetchmany(10_000)
            if not chunk:
                break
            for r in chunk[: EXCEL_MAX_ROWS - written]:
                ws.append([_excel_value(v) for v in r])
            written += len(chunk)
        if written >= EXCEL_MAX_ROWS and cur.fetchone() is not None:
            ws.append([f"… cut at {EXCEL_MAX_ROWS:,} rows (Excel's limit): export as CSV or Parquet for everything"])
    wb.save(out)


def _excel_value(v: Any) -> Any:
    if hasattr(v, "tzinfo") and getattr(v, "tzinfo", None) is not None:
        return v.replace(tzinfo=None)  # Excel has no time zones
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    if v is None or isinstance(v, (int, float, str, bool, decimal.Decimal, dt.date, dt.time, dt.timedelta)):
        return v
    return str(v)  # lists, structs, UUIDs ...


def remove_folder(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
