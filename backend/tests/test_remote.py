"""Remote intake end to end through the API with the thread runner: S3 (moto, in-process), HTTP (http.server) and
SFTP (an in-process paramiko server). No external services."""
import gzip
import http.server
import io
import sqlite3
import threading
import time
import zipfile
from datetime import timedelta
from pathlib import Path

import boto3
import duckdb
import pytest
from moto import mock_aws

from jobhelpers import add_conn, run, uniq, wait_job
from sftpstub import SFTPStub

CSV1 = "id,name,amount\n1,a,10\n2,b,20\n"
CSV2 = "id,name,amount\n3,c,30\n4,d,40\n"
CSV3 = "id,name,amount\n5,e,50\n6,f,oops\n"  # one row that cannot be converted


@pytest.fixture()
def s3():
    with mock_aws():
        c = boto3.client("s3", region_name="us-east-1")
        c.create_bucket(Bucket="lake")
        yield c


def put(s3, key, body, pause=0.0):
    s3.put_object(Bucket="lake", Key=key, Body=body.encode() if isinstance(body, str) else body)
    if pause:
        time.sleep(pause)  # S3 LastModified has 1 s resolution in moto: keep "newest" unambiguous


@pytest.fixture()
def conns(client, auth, env, s3):
    src = add_conn(client, auth, "s3", uniq("s3"), {"bucket": "lake", "region": "us-east-1", "prefix": "drop"},
                   {"access_key_id": "AKID", "secret_access_key": "secret"})
    dst = add_conn(client, auth, "sqlite", uniq("dst"), {"path": str(env / "dst.sqlite"), "create_if_missing": True})
    return src, dst


def settings(monkeypatch, **kw):
    from app.config import get_settings

    for k, v in kw.items():
        monkeypatch.setenv(f"CONDUIT_{k.upper()}", str(v))
    get_settings.cache_clear()


def body(src, dst, path, match="exact", table="t", mode="append", skip=True, **kw):
    cols = kw.pop("columns", [{"source": "id", "type": "INTEGER"}, {"source": "name"}, {"source": "amount", "type": "DOUBLE"}])
    return {"source": {"remote": {"connection_id": src, "path": path, "match": match, "skip_loaded": skip, **kw.pop("remote", {})}},
            "target": {"connection_id": dst, "table": table, "mode": mode}, "columns": cols,
            "options": {"max_rejects": -1, "workers": 1, **kw.pop("options", {})}, **kw}


def rows(env, table="t"):
    c = sqlite3.connect(env / "dst.sqlite")
    try:
        return sorted(r[0] for r in c.execute(f"select id from {table}"))
    finally:
        c.close()


def job_dir(job_id) -> Path:
    from app.remote import job_dir as jd
    return jd(job_id)


# ---- browsing and matching -----------------------------------------------------------------------

def test_list_folder(client, auth, conns, s3):
    src, _ = conns
    for key in ("drop/a.csv", "drop/b.csv.gz", "drop/c.zip", "drop/.hidden.csv", "drop/notes.md", "drop/sub/x.csv"):
        put(s3, key, "x")
    entries = client.get(f"/api/remote/{src}/list", headers=auth).json()
    assert [e["name"] for e in entries] == ["sub", "a.csv", "b.csv.gz", "c.zip", "notes.md"]
    by = {e["name"]: e for e in entries}
    assert by["sub"]["is_dir"] and by["a.csv"]["format"] == "csv" and by["a.csv"]["size"] == 1
    assert (by["b.csv.gz"]["format"], by["b.csv.gz"]["compression"]) == ("csv", "gzip")
    assert (by["c.zip"]["format"], by["c.zip"]["compression"]) == (None, "zip") and by["notes.md"]["format"] is None
    assert [e["name"] for e in client.get(f"/api/remote/{src}/list", params={"path": "sub"}, headers=auth).json()] == ["x.csv"]


def test_glob_patterns(client, auth, conns, s3):
    src, _ = conns
    for key in ("drop/2024/a.csv", "drop/2024/b.csv", "drop/2024/deep/c.csv", "drop/2025/d.csv", "drop/e1.csv", "drop/e22.csv"):
        put(s3, key, CSV1)

    def matched(path):
        r = client.post("/api/remote/match", json={"remote": {"connection_id": src, "path": path, "match": "all"}}, headers=auth)
        assert r.status_code == 200, r.text
        return [f["path"] for f in r.json()["files"]]

    assert matched("2024/*.csv") == ["2024/a.csv", "2024/b.csv"]  # * stays in one folder
    assert matched("2024/**/*.csv") == ["2024/a.csv", "2024/b.csv", "2024/deep/c.csv"]  # ** spans folders
    assert matched("*/*.csv") == ["2024/a.csv", "2024/b.csv", "2025/d.csv"]
    assert matched("e?.csv") == ["e1.csv"]
    assert matched("nothing/*.csv") == []
    r = client.post("/api/remote/match", json={"remote": {"connection_id": src, "path": "../x/*.csv", "match": "all"}}, headers=auth)
    assert r.status_code == 422 and ".." in r.text
    r = client.post("/api/remote/match", json={"remote": {"connection_id": src, "path": "2024/*.csv", "match": "exact"}}, headers=auth)
    assert r.status_code == 422 and "pattern" in r.text


def test_newest_and_status(client, auth, conns, s3):
    src, dst = conns
    put(s3, "drop/orders_1.csv", CSV1, pause=1.1)
    put(s3, "drop/orders_2.csv", CSV2)
    m = client.post("/api/remote/match", json={"remote": {"connection_id": src, "path": "orders_*.csv", "match": "newest"}}, headers=auth).json()
    assert m["chosen"] == ["orders_2.csv"] and m["total"] == 2


# ---- loading ---------------------------------------------------------------------------------------

def test_exact_csv_into_sqlite(client, auth, conns, s3, env):
    src, dst = conns
    put(s3, "drop/orders.csv", CSV1)
    insp = client.post("/api/remote/inspect", json={"remote": {"connection_id": src, "path": "orders.csv"}}, headers=auth).json()
    assert [c["name"] for c in insp["columns"]] == ["id", "name", "amount"] and insp["row_estimate"] == 2
    assert insp["path"] == "s3://lake/drop/orders.csv"
    j = run(client, auth, body(src, dst, "orders.csv"))
    assert j["status"] == "succeeded" and j["rows_written"] == 2, j
    assert j["name"] == "s3://lake/drop/orders.csv → t" and j["kind"] == "file_load"
    assert rows(env) == [1, 2]
    assert not job_dir(j["id"]).exists()  # downloads are deleted as soon as the job succeeds


def test_all_files_one_split_each_rejects_name_the_file(client, auth, conns, s3, env):
    src, dst = conns
    for i, csv in enumerate((CSV1, CSV2, CSV3), 1):
        put(s3, f"drop/batch/part_{i}.csv", csv)
    j = run(client, auth, body(src, dst, "batch/part_*.csv", "all"))
    assert j["status"] == "succeeded" and j["rows_written"] == 5 and j["rows_rejected"] == 1, j
    assert j["splits_total"] == 3
    detail = client.get(f"/api/jobs/{j['id']}", headers=auth).json()
    assert [s["label"] for s in detail["splits"]] == ["batch/part_1.csv", "batch/part_2.csv", "batch/part_3.csv"]
    rej = client.get(f"/api/jobs/{j['id']}/rejects", headers=auth).text
    assert "batch/part_3.csv: column amount" in rej
    # a fresh run with skip: nothing new, a 0-row success with the note
    j2 = run(client, auth, body(src, dst, "batch/part_*.csv", "all"))
    assert j2["status"] == "succeeded" and j2["rows_written"] == 0, j2
    log = " ".join(line["msg"] for line in client.get(f"/api/jobs/{j2['id']}", headers=auth).json()["log"])
    assert "No new files matching batch/part_*.csv" in log
    # a new file: only that one loads; a file overwritten in place shows as "changed" first
    put(s3, "drop/batch/part_4.csv", "id,name,amount\n7,g,70\n")
    put(s3, "drop/batch/part_1.csv", "id,name,amount\n1,a,10\n2,b,20\n8,h,80\n")
    m = client.post("/api/remote/match", json={"remote": {"connection_id": src, "path": "batch/part_*.csv", "match": "all"},
                                               "target": {"connection_id": dst, "table": "t"}}, headers=auth).json()
    status = {f["path"]: f["status"] for f in m["files"]}
    assert status == {"batch/part_1.csv": "changed", "batch/part_2.csv": "loaded", "batch/part_3.csv": "loaded",
                      "batch/part_4.csv": "new"}, status
    assert (m["new_count"], m["changed_count"], m["loaded_count"]) == (1, 1, 2)
    assert m["chosen"] == ["batch/part_1.csv", "batch/part_4.csv"]
    assert any("changed since they were loaded" in n for n in m["notes"])


def test_all_with_mismatched_columns_fails_before_writing(client, auth, conns, s3, env):
    src, dst = conns
    put(s3, "drop/mix/a.csv", CSV1)
    put(s3, "drop/mix/b.csv", "id,label\n1,x\n")
    j = run(client, auth, body(src, dst, "mix/*.csv", "all"))
    assert j["status"] == "failed" and "mix/b.csv does not have the same columns as mix/a.csv" in j["error"], j
    assert "missing amount, name" in j["error"] and "extra label" in j["error"]


def test_replace_with_all_ignores_skip(client, auth, conns, s3, env):
    src, dst = conns
    put(s3, "drop/r/a.csv", CSV1)
    put(s3, "drop/r/b.csv", CSV2)
    plan = client.post("/api/jobs/plan", json=body(src, dst, "r/*.csv", "all", mode="replace"), headers=auth).json()
    assert any("ignored" in n for n in plan["notes"]), plan["notes"]
    for _ in range(2):
        j = run(client, auth, body(src, dst, "r/*.csv", "all", mode="replace"))
        assert j["status"] == "succeeded" and j["rows_written"] == 4, j
    assert rows(env) == [1, 2, 3, 4]  # every run replaced the target with every matching file


def test_marked_loaded_per_file_even_when_the_job_fails_and_retry_reuses_the_manifest(client, auth, conns, s3, env, monkeypatch):
    from app.engine.filesource import FileAdapter

    src, dst = conns
    put(s3, "drop/m/a.csv", CSV1)
    put(s3, "drop/m/b.csv", "id,name,amount\n3,c,30\n")
    real = FileAdapter.read_batches
    broken = {"m/b.csv"}

    def flaky(self, req, split, batch_rows):  # the second file fails once, before any of its rows is read
        if self.label in broken:
            broken.discard(self.label)
            raise RuntimeError("disk read error")
        yield from real(self, req, split, batch_rows)

    monkeypatch.setattr(FileAdapter, "read_batches", flaky)
    spec = body(src, dst, "m/*.csv", "all", options={"retries": 0})
    j = run(client, auth, spec)
    assert j["status"] == "failed" and "disk read error" in j["error"], j
    assert rows(env) == [1, 2]
    assert (job_dir(j["id"]) / "manifest.json").exists()  # kept for Retry
    # a new file arrives before the Retry: the retry loads exactly the job's original files, nothing else
    put(s3, "drop/m/c.csv", CSV2)
    assert client.post(f"/api/jobs/{j['id']}/retry", headers=auth).status_code == 200
    j = wait_job(client, auth, j["id"])
    assert j["status"] == "succeeded" and rows(env) == [1, 2, 3], j
    assert not job_dir(j["id"]).exists()
    # a.csv was recorded when its split finished (during the failed run), b.csv during the retry:
    # a fresh run with skip loads only c.csv
    j2 = run(client, auth, spec)
    assert j2["status"] == "succeeded" and rows(env) == [1, 2, 3, 3, 4], j2
    detail = client.get(f"/api/jobs/{j2['id']}", headers=auth).json()
    assert [s["label"] for s in detail["splits"]] == ["m/c.csv"]


def test_a_fresh_run_after_a_failed_one_skips_the_files_it_finished(client, auth, conns, s3, env, monkeypatch):
    from app.engine.filesource import FileAdapter

    src, dst = conns
    for i, csv in enumerate((CSV1, CSV2, "id,name,amount\n9,z,90\n"), 1):
        put(s3, f"drop/f/{i}.csv", csv)
    real = FileAdapter.read_batches

    def fail_third(self, req, split, batch_rows):
        if self.label == "f/3.csv":
            raise RuntimeError("network gone")
        yield from real(self, req, split, batch_rows)

    monkeypatch.setattr(FileAdapter, "read_batches", fail_third)
    spec = body(src, dst, "f/*.csv", "all", options={"retries": 0})
    assert run(client, auth, spec)["status"] == "failed"
    monkeypatch.setattr(FileAdapter, "read_batches", real)
    j = run(client, auth, spec)  # a new job, not a Retry: the two finished files are not loaded again
    assert j["status"] == "succeeded" and j["rows_written"] == 1 and rows(env) == [1, 2, 3, 4, 9], j


def test_file_offset_resume_skips_committed_rows(env):
    from app.engine.filesource import FileAdapter, FileSpec
    from app.engine.model import ReadRequest

    p = env / "big.csv"
    p.write_text("id\n" + "\n".join(str(i) for i in range(1, 11)) + "\n")
    a = FileAdapter(FileSpec(path=str(p)))
    split = a.plan_splits(ReadRequest(), 1)[0]
    assert split.kind == "file" and split.resumable
    split.extra["offset"] = 7
    batches = list(a.read_batches(ReadRequest(columns=a.read_columns()), split, 2))
    got = [v for b in batches for v in b.table.column("id").to_pylist()]
    assert got == ["8", "9", "10"] and batches[-2].resume == 10


def test_overlapping_loads_are_refused(client, auth, conns, s3, env):
    from app.db import get_sessionmaker
    from app.models import Job, utcnow

    src, dst = conns
    put(s3, "drop/o/a.csv", CSV1)
    spec = body(src, dst, "o/*.csv", "all")
    with get_sessionmaker()() as s:
        s.add(Job(name="other", kind="file_load", status="running", spec=spec, heartbeat_at=utcnow()))
        s.commit()
    j = run(client, auth, spec)
    assert j["status"] == "failed" and "Another job (other, running) is loading from this source" in j["error"], j


def test_disk_space_is_checked_before_downloading(client, auth, conns, s3, env, monkeypatch):
    import shutil as sh

    from app.remote import job_source
    from app.engine.spec import RemoteIn
    from app.db import get_sessionmaker
    import uuid

    src, dst = conns
    put(s3, "drop/space.csv", CSV1)
    real = sh.disk_usage
    monkeypatch.setattr("app.filecache.shutil.disk_usage", lambda p: real(p)._replace(free=10))
    # the request fails fast (its preview needs the space too) ...
    r = client.post("/api/jobs", json=body(src, dst, "space.csv"), headers=auth)
    assert r.status_code == 422 and "Not enough free disk space" in r.text, r.text
    # ... and so does the job's own download, before fetching a byte
    with get_sessionmaker()() as s, pytest.raises(Exception, match="Not enough free disk space for downloading 1 file"):
        job_source(s, uuid.uuid4(), RemoteIn(connection_id=src, path="space.csv"), f"{dst}:t", "append")


def test_size_cap(client, auth, conns, s3, env, monkeypatch):
    src, dst = conns
    put(s3, "drop/huge.csv", "id\n" + "1\n" * 600_000)  # 1.2 MB
    settings(monkeypatch, remote_max_file_mb=1)
    r = client.post("/api/jobs", json=body(src, dst, "huge.csv", columns=[{"source": "id"}]), headers=auth)
    assert r.status_code == 422 and "1 MB limit" in r.text, r.text


def test_gz_and_zip_from_s3(client, auth, conns, s3, env):
    src, dst = conns
    put(s3, "drop/z/orders.csv.gz", gzip.compress(CSV1.encode()))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("export/orders.csv", CSV2)
        zf.writestr("__MACOSX/export/._orders.csv", "junk")
    put(s3, "drop/z/orders.zip", buf.getvalue())
    for path, table in (("z/orders.csv.gz", "gz"), ("z/orders.zip", "zp")):
        j = run(client, auth, body(src, dst, path, table=table))
        assert j["status"] == "succeeded" and j["rows_written"] == 2, (path, j)
    two = io.BytesIO()
    with zipfile.ZipFile(two, "w") as zf:
        zf.writestr("a.csv", CSV1)
        zf.writestr("b.csv", CSV2)
    put(s3, "drop/z/two.zip", two.getvalue())
    r = client.post("/api/jobs/plan", json=body(src, dst, "z/two.zip", table="x"), headers=auth)
    assert r.status_code == 422 and "exactly one" in r.text


def test_sampled_previews(client, auth, conns, s3, env, monkeypatch):
    src, _ = conns
    settings(monkeypatch, remote_inspect_full_mb=0)  # everything counts as "big": previews sample
    put(s3, "drop/big.csv", "id,v\n" + "".join(f"{i},x\n" for i in range(5000)))
    put(s3, "drop/big.csv.gz", gzip.compress(("id,v\n" + "".join(f"{i},x\n" for i in range(5000))).encode()))
    pq = env / "p.parquet"
    duckdb.sql(f"COPY (SELECT range AS id FROM range(12345)) TO '{pq}' (FORMAT parquet, ROW_GROUP_SIZE 1000)")
    put(s3, "drop/big.parquet", pq.read_bytes())
    for path in ("big.csv", "big.csv.gz"):
        b = client.post("/api/remote/inspect", json={"remote": {"connection_id": src, "path": path}}, headers=auth).json()
        assert b["row_estimate"] is None and b["remote"]["file"]["sampled"], b
        assert any("counted during the load" in n for n in b["notes"]) and [c["name"] for c in b["columns"]] == ["id", "v"]
    monkeypatch.setattr("app.remote.BIG_ROW_GROUP", 0)  # also take the buffered-stream path
    b = client.post("/api/remote/inspect", json={"remote": {"connection_id": src, "path": "big.parquet"}}, headers=auth).json()
    assert b["row_estimate"] == 12345 and b["exact"] and len(b["preview"]) == 20, b


# ---- HTTP -------------------------------------------------------------------------------------------

@pytest.fixture()
def web(env):
    root = env / "www"
    root.mkdir()
    (root / "orders.csv").write_text(CSV1)
    (root / "export").write_text(CSV2)  # no extension

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(root), **k)

        def log_message(self, *a):
            pass

        def send_head(self):
            if self.path.startswith("/go"):
                self.send_response(302)
                self.send_header("Location", "/orders.csv")
                self.end_headers()
                return None
            return super().send_head()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def url_body(dst, url, table, **remote):
    return {"source": {"remote": {"path": url, **remote}}, "target": {"connection_id": dst, "table": table},
            "options": {"max_rejects": -1}}


def test_http_urls(client, auth, env, web):
    dst = add_conn(client, auth, "sqlite", uniq("dst"), {"path": str(env / "dst.sqlite"), "create_if_missing": True})
    j = run(client, auth, url_body(dst, f"{web}/orders.csv?token=SECRET123", "h1"))
    assert j["status"] == "succeeded" and j["rows_written"] == 2, j
    detail = client.get(f"/api/jobs/{j['id']}", headers=auth).json()
    shown = detail["name"] + " ".join(line["msg"] for line in detail["log"]) + str(detail["plan"])
    assert "SECRET123" not in shown and detail["name"] == f"{web}/orders.csv → h1"
    j = run(client, auth, url_body(dst, f"{web}/go/now", "h2"))  # a redirect to the file
    assert j["status"] == "succeeded" and j["rows_written"] == 2, j
    r = client.post("/api/jobs/plan", json=url_body(dst, f"{web}/missing.csv?sig=SECRET", "h3"), headers=auth)
    assert r.status_code == 422 and "404" in r.text and "SECRET" not in r.text
    r = client.post("/api/remote/inspect", json={"remote": {"path": f"{web}/export"}}, headers=auth)
    assert r.status_code == 422 and "choose the file format" in r.text
    j = run(client, auth, url_body(dst, f"{web}/export", "h4", format="csv"))
    assert j["status"] == "succeeded" and j["rows_written"] == 2, j
    r = client.post("/api/jobs/plan", json=url_body(dst, "ftp://example.com/a.csv", "h5"), headers=auth)
    assert r.status_code == 422


# ---- SFTP --------------------------------------------------------------------------------------------

def test_sftp_newest(client, auth, env):
    root = env / "sftp"
    (root / "in").mkdir(parents=True)
    (root / "in" / "a.csv").write_text(CSV1)
    import os
    os.utime(root / "in" / "a.csv", (time.time() - 100, time.time() - 100))
    (root / "in" / "b.csv").write_text(CSV2)
    stub = SFTPStub(root)
    try:
        src = add_conn(client, auth, "sftp", uniq("sftp"), {"host": "127.0.0.1", "port": stub.port, "user": stub.user, "base_path": "/in"},
                       {"password": stub.password})
        dst = add_conn(client, auth, "sqlite", uniq("dst"), {"path": str(env / "dst.sqlite"), "create_if_missing": True})
        j = run(client, auth, body(src, dst, "*.csv", "newest"))
        assert j["status"] == "succeeded" and rows(env) == [3, 4], j
        conn = client.get(f"/api/connections/{src}", headers=auth).json()
        assert conn["config"]["host_key_sha256"].startswith("SHA256:")  # learned while reading
        j = run(client, auth, body(src, dst, "*.csv", "newest"))
        assert j["status"] == "succeeded" and j["rows_written"] == 0, j  # the newest is already loaded
    finally:
        stub.close()


# ---- pipelines, the scheduler and Compare -------------------------------------------------------------

def test_pipeline_with_remote_source_and_scheduled_check(client, auth, conns, s3, env):
    from app.db import get_sessionmaker
    from app.engine.schedule import run_due
    from app.models import Job, Pipeline, utcnow

    src, dst = conns
    put(s3, "drop/daily/d1.csv", CSV1)
    r = client.post("/api/pipelines", json={"name": uniq("p"), "source": {"remote": {"connection_id": src, "path": "daily/*.csv", "match": "newest"}},
                                            "target": {"connection_id": dst, "table": "pt"}}, headers=auth)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    pv = client.post(f"/api/pipelines/{pid}/preview", json={"sample_rows": 50}, headers=auth).json()
    assert [c["name"] for c in pv["columns"]] == ["id", "name", "amount"] and pv["sampled_rows"] == 2
    j = wait_job(client, auth, client.post(f"/api/pipelines/{pid}/run", headers=auth).json()["id"])
    assert j["status"] == "succeeded" and j["rows_written"] == 2
    # a manual run with nothing new: a 0-row job
    j = wait_job(client, auth, client.post(f"/api/pipelines/{pid}/run", headers=auth).json()["id"])
    assert j["status"] == "succeeded" and j["rows_written"] == 0
    # the scheduler: nothing new -> no job, a note on the pipeline
    client.patch(f"/api/pipelines/{pid}", json={"schedule": "* * * * *", "enabled": True}, headers=auth)
    with get_sessionmaker()() as s:
        s.get(Pipeline, uuid_of(pid)).next_run_at = utcnow() - timedelta(minutes=1)
        s.commit()
        before = s.query(Job).count()
    assert run_due() == 0
    p = client.get(f"/api/pipelines/{pid}", headers=auth).json()
    assert p["last_checked_at"] and "No new files matching daily/*.csv" in p["last_check_note"], p
    with get_sessionmaker()() as s:
        assert s.query(Job).count() == before
        s.get(Pipeline, uuid_of(pid)).next_run_at = utcnow() - timedelta(minutes=1)
        s.commit()
    put(s3, "drop/daily/d2.csv", CSV2)
    assert run_due() == 1
    p = client.get(f"/api/pipelines/{pid}", headers=auth).json()
    assert p["last_check_note"].startswith("load started: 1 file(s)"), p
    j = wait_job(client, auth, p["last_run"]["job_id"])
    assert j["status"] == "succeeded" and rows(env, "pt") == [1, 2, 3, 4]


def uuid_of(s):
    import uuid
    return uuid.UUID(s)


def test_compare_with_a_remote_side(client, auth, conns, s3, env):
    src, dst = conns
    put(s3, "drop/cmp/a.csv", CSV1)
    put(s3, "drop/cmp/b.csv", CSV2)
    c = sqlite3.connect(env / "dst.sqlite")
    c.executescript("create table ct(id integer primary key, name text, amount double); insert into ct values (1,'a',10),(2,'b',21),(3,'c',30);")
    c.close()
    r = client.post("/api/compare", json={"source": {"remote": {"connection_id": src, "path": "cmp/*.csv", "match": "all"}},
                                          "target": {"connection_id": dst, "table": "ct"}, "keys": ["id"]}, headers=auth)
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    for _ in range(200):
        cmp = client.get(f"/api/compare/{cid}", headers=auth).json()
        if cmp["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.1)
    assert cmp["status"] == "succeeded", cmp
    sm = cmp["summary"]
    assert (sm["rows_source"], sm["missing"], sm["mismatched"], sm["extra"]) == (4, 1, 1, 0), sm
    from app.compare.engine import folder
    assert not (folder(cid) / "remote").exists()  # the downloaded side is gone after the diff


def test_editing_the_prefix_does_not_make_loaded_files_look_new(client, auth, conns, s3, env):
    src, dst = conns
    put(s3, "drop/keep/a.csv", CSV1)
    assert run(client, auth, body(src, dst, "keep/*.csv", "all"))["rows_written"] == 2
    client.patch(f"/api/connections/{src}", json={"config": {"prefix": ""}}, headers=auth)  # was "drop"
    m = client.post("/api/remote/match", json={"remote": {"connection_id": src, "path": "drop/keep/*.csv", "match": "all"},
                                               "target": {"connection_id": dst, "table": "t"}}, headers=auth).json()
    assert [f["status"] for f in m["files"]] == ["loaded"] and m["chosen"] == [], m


def test_s3_credential_errors_are_explained():
    from botocore.exceptions import ClientError

    from app.connectors.s3 import S3Connector

    c = S3Connector({"bucket": "lake"}, {})
    for code, text in (("InvalidAccessKeyId", "access key ID is not valid"), ("SignatureDoesNotMatch", "secret access key is wrong"),
                       ("AccessDenied", "Access denied to bucket lake")):
        err = ClientError({"Error": {"Code": code, "Message": "x"}}, "ListObjectsV2")
        assert text in str(c._explain(err, "Listing")), code
