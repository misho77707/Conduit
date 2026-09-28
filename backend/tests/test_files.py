import json

import pytest

from app.connectors.paths import PathNotAllowed
from app.files import UnsupportedFormat, inspect_file


def test_inspect_csv(env):
    p = env / "a.csv"
    p.write_text("id,name\n1,alice\n2,bob\n3,carol\n")
    r = inspect_file(str(p), sample_rows=2)
    assert r.format == "csv" and r.exact and r.row_estimate == 3
    assert [c.name for c in r.columns] == ["id", "name"]
    assert len(r.preview) == 2 and r.preview[0]["name"] == "alice"


def test_inspect_ndjson(env):
    p = env / "a.ndjson"
    p.write_text("\n".join(json.dumps({"id": i, "name": f"n{i}"}) for i in range(5)) + "\n")
    r = inspect_file(str(p))
    assert r.format == "json" and r.row_estimate == 5 and r.exact
    assert {c.name for c in r.columns} == {"id", "name"}


def test_inspect_parquet(env):
    import duckdb
    p = env / "a.parquet"
    duckdb.sql("select range as id, 'x' as name from range(10)").write_parquet(str(p))
    r = inspect_file(str(p))
    assert r.format == "parquet" and r.exact and r.row_estimate == 10


def test_inspect_excel(env):
    from openpyxl import Workbook
    p = env / "a.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["id", "name"])
    ws.append([1, "alice"])
    ws.append([2, "bob"])
    wb.save(p)
    r = inspect_file(str(p))
    assert r.format == "excel" and r.row_estimate == 2 and r.exact
    assert {c.name for c in r.columns} == {"id", "name"}
    assert r.preview[0]["name"] == "alice"


def test_unsupported_extension(env):
    p = env / "a.exe"
    p.write_bytes(b"x")
    with pytest.raises(UnsupportedFormat):
        inspect_file(str(p))


def test_path_outside_allowed_roots(env):
    with pytest.raises(PathNotAllowed):
        inspect_file(str(env.parent / "outside.csv"))


def test_missing_file(env):
    with pytest.raises(FileNotFoundError):
        inspect_file(str(env / "nope.csv"))


def test_large_csv_skips_exact_count(env, monkeypatch):
    import app.files as files_mod
    monkeypatch.setattr(files_mod, "_EXACT_COUNT_MAX_BYTES", 10)  # force the "too big to pre-count" branch
    p = env / "big.csv"
    p.write_text("id\n" + "\n".join(str(i) for i in range(50)))
    r = inspect_file(str(p))
    assert not r.exact and r.row_estimate is None and r.notes
