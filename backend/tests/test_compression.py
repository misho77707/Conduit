"""Compressed files as sources: .gz text read directly, .zip with exactly one data file unpacked into the cache.
Through the same upload / server-folder paths the wizard uses."""
import gzip
import sqlite3
import zipfile

import duckdb
import pytest

from jobhelpers import add_conn, run, uniq

CSV = "id,name,amount\n1,a,10.5\n2,b,20\n3,c,30\n"


def _gz(path, text):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(text)
    return path


def _zip(path, members: dict):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def _inspect(client, auth, path):
    return client.post("/api/files/inspect", json={"path": str(path)}, headers=auth)


def test_gzip_text_formats(client, auth, env):
    for name, text, cols, rows in (("o.csv.gz", CSV, ["id", "name", "amount"], 3),
                                   ("o.tsv.gz", CSV.replace(",", "\t"), ["id", "name", "amount"], 3),
                                   ("o.ndjson.gz", '{"id": 1}\n{"id": 2}\n', ["id"], 2),
                                   ("o.json.gz", '[{"id": 1}, {"id": 2}]', ["id"], 2),
                                   ("o.jsonl.gz", '{"id": 1}\n', ["id"], 1)):
        r = _inspect(client, auth, _gz(env / name, text))
        assert r.status_code == 200, (name, r.text)
        body = r.json()
        assert body["compression"] == "gzip" and [c["name"] for c in body["columns"]] == cols, (name, body)
        assert body["exact"] and body["row_estimate"] == rows, (name, body)


def test_compressed_parquet_or_excel_is_refused(client, auth, env):
    (env / "x.parquet.gz").write_bytes(b"x")
    r = _inspect(client, auth, env / "x.parquet.gz")
    assert r.status_code == 422 and "compressed already" in r.text


def test_zip_with_one_data_file(client, auth, env):
    pq = env / "inner.parquet"
    duckdb.sql(f"COPY (SELECT range AS id FROM range(5)) TO '{pq}' (FORMAT parquet)")
    for name, members, fmt, rows in (
        ("csv.zip", {"data/orders.csv": CSV, "__MACOSX/data/._orders.csv": "junk", ".DS_Store": "junk", "README": "notes"}, "csv", 3),
        ("pq.zip", {"inner.parquet": pq.read_bytes()}, "parquet", 5),
    ):
        r = _inspect(client, auth, _zip(env / name, members))
        assert r.status_code == 200, (name, r.text)
        b = r.json()
        assert (b["format"], b["compression"], b["row_estimate"]) == (fmt, "zip", rows), b
        assert any("inside the zip" in n for n in b["notes"])


@pytest.mark.parametrize("members", [{"a.csv": CSV, "b.csv": CSV}, {"README.txt.md": "x"}, {}])
def test_zip_must_hold_exactly_one_data_file(client, auth, env, members):
    r = _inspect(client, auth, _zip(env / "bad.zip", members))
    assert r.status_code == 422 and "exactly one" in r.text, r.text


def test_not_a_zip(client, auth, env):
    (env / "fake.zip").write_bytes(b"not a zip")
    r = _inspect(client, auth, env / "fake.zip")
    assert r.status_code == 422 and "not a valid zip" in r.text


def test_folder_listing_shows_format_through_compression(client, auth, env):
    _gz(env / "a.csv.gz", CSV)
    _zip(env / "b.zip", {"b.json": '{"id": 1}\n'})
    _zip(env / "c.zip", {"1.csv": CSV, "2.csv": CSV})
    entries = {e["name"]: e for e in client.get("/api/files/list", headers=auth).json()}
    assert (entries["a.csv.gz"]["format"], entries["a.csv.gz"]["compression"]) == ("csv", "gzip")
    assert (entries["b.zip"]["format"], entries["b.zip"]["compression"]) == ("json", "zip")
    assert entries["c.zip"]["format"] is None  # two data files: not loadable


def test_load_gz_and_zip_into_sqlite(client, auth, env):
    dst = add_conn(client, auth, "sqlite", uniq("dst"), {"path": str(env / "dst.sqlite"), "create_if_missing": True})
    bad = CSV + "4,d,not-a-number\n"
    for name, path in (("gz", _gz(env / "o.csv.gz", bad)), ("zip", _zip(env / "o.zip", {"x/orders.csv": bad}))):
        body = {"source": {"file": {"path": str(path)}}, "target": {"connection_id": dst, "table": f"t_{name}"},
                "columns": [{"source": "id", "type": "INTEGER"}, {"source": "name"}, {"source": "amount", "type": "DOUBLE"}],
                "options": {"max_rejects": -1}}
        j = run(client, auth, body)
        assert j["status"] == "succeeded" and j["rows_written"] == 3 and j["rows_rejected"] == 1, j
        c = sqlite3.connect(env / "dst.sqlite")
        assert c.execute(f"select sum(amount) from t_{name}").fetchone()[0] == 60.5
        c.close()


def test_upload_a_gz_file(client, auth, env):
    r = client.post("/api/files/upload", files={"file": ("orders.csv.gz", gzip.compress(CSV.encode()), "application/gzip")}, headers=auth)
    assert r.status_code == 201, r.text
    b = _inspect(client, auth, r.json()["path"]).json()
    assert (b["format"], b["compression"], b["row_estimate"]) == ("csv", "gzip", 3)
