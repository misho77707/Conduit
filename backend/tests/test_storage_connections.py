"""File-storage connection types (S3, SFTP, HTTP): saving, testing, host-key trust on first use, and keeping
them out of every place that needs a database. S3 runs against moto (in-process), SFTP against a local stub
server, HTTP against http.server - no external services."""
import http.server
import threading

import boto3
import paramiko
import pytest
from moto import mock_aws

from jobhelpers import add_conn, uniq
from sftpstub import SFTPStub


@pytest.fixture()
def s3():
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="lake")
        yield boto3.client("s3", region_name="us-east-1")


S3_CFG = {"bucket": "lake", "region": "us-east-1", "prefix": "exports"}
S3_SECRETS = {"access_key_id": "AKIDEXAMPLE", "secret_access_key": "s3cr3t-value"}


def test_types_carry_a_category(client, auth):
    types = {t["type"]: t for t in client.get("/api/meta/db-types", headers=auth).json()}
    assert {k for k, t in types.items() if t["category"] == "storage"} == {"s3", "sftp", "http"}
    assert types["postgres"]["category"] == "database"
    assert "secret_access_key" in types["s3"]["secret_fields"] and "private_key" in types["sftp"]["secret_fields"]


def test_s3_connection_save_test_and_edit_keeps_the_secret(client, auth, s3):
    r = client.post("/api/connections/test", json={"type": "s3", "config": S3_CFG, "secrets": S3_SECRETS}, headers=auth)
    assert r.json()["ok"] is True and "no objects" in r.json()["detail"], r.text
    s3.put_object(Bucket="lake", Key="exports/a.csv", Body=b"id\n1\n")
    cid = add_conn(client, auth, "s3", uniq("s3"), S3_CFG, S3_SECRETS)
    c = client.get(f"/api/connections/{cid}", headers=auth).json()
    assert c["host"] == "s3://lake/exports" and c["category"] == "storage"
    assert "s3cr3t" not in str(c)  # secrets are never returned
    r = client.post(f"/api/connections/{cid}/test", headers=auth).json()
    assert r["ok"] and "objects found" in r["detail"]
    # edit without the secret: the stored keys stay
    r = client.patch(f"/api/connections/{cid}", json={"config": {**S3_CFG, "prefix": ""}, "secrets": {"secret_access_key": ""}}, headers=auth)
    assert r.status_code == 200 and r.json()["host"] == "s3://lake"
    assert client.post(f"/api/connections/{cid}/test", headers=auth).json()["ok"] is True


def test_s3_test_reports_a_missing_bucket_clearly(client, auth, s3):
    r = client.post("/api/connections/test", json={"type": "s3", "config": {"bucket": "nope", "region": "us-east-1"}, "secrets": S3_SECRETS}, headers=auth).json()
    assert r["ok"] is False and r["error"] == "Bucket nope does not exist"


def test_s3_validation(client, auth):
    r = client.post("/api/connections", json={"name": "x", "type": "s3", "config": {"region": "eu-west-1"}, "secrets": {"access_key_id": "a"}}, headers=auth)
    assert r.status_code == 422 and "Bucket is required" in r.text and "secret access key" in r.text


def test_storage_connections_are_never_databases(client, auth, s3, env):
    cid = add_conn(client, auth, "s3", uniq("s3"), S3_CFG, S3_SECRETS)
    assert client.get(f"/api/connections/{cid}/tables", headers=auth).status_code == 422
    assert client.get(f"/api/connections/{cid}/tables/x/inspect", headers=auth).status_code == 422
    import sqlite3
    sqlite3.connect(env / "x.sqlite").execute("create table t(id integer)").connection.commit()
    db = add_conn(client, auth, "sqlite", uniq("db"), {"path": str(env / "x.sqlite"), "create_if_missing": True})
    job = {"source": {"connection_id": db, "table": "t"}, "target": {"connection_id": cid, "table": "t"}}
    r = client.post("/api/jobs", json=job, headers=auth)
    assert r.status_code == 422 and "file storage" in r.text
    r = client.post("/api/jobs/plan", json={**job, "source": {"connection_id": cid, "table": "t"}, "target": {"connection_id": db, "table": "t"}}, headers=auth)
    assert r.status_code == 422 and "file storage" in r.text
    r = client.post("/api/pipelines", json={"name": uniq("p"), "source": {"connection_id": db, "table": "t"},
                                            "target": {"connection_id": cid, "table": "t"}}, headers=auth)
    assert r.status_code == 422 and "file storage" in r.text
    r = client.post("/api/monitor/checks", json={"name": "q", "kind": "query", "connection_id": cid, "query": "select 1"}, headers=auth)
    assert r.status_code == 422 and "file storage" in r.text
    r = client.post("/api/compare/columns", json={"source": {"connection_id": cid, "table": "t"}, "target": {"connection_id": db, "table": "t"}}, headers=auth)
    assert r.status_code == 422 and "file storage" in r.text


# ---- SFTP: password / key auth and host keys trusted on first use --------------------------------------

@pytest.fixture()
def sftp(env):
    root = env / "sftp-root"
    (root / "drop").mkdir(parents=True)
    (root / "drop" / "a.csv").write_text("id\n1\n")
    stub = SFTPStub(root)
    yield stub
    stub.close()


def _sftp_cfg(stub, **kw):
    return {"host": "127.0.0.1", "port": stub.port, "user": stub.user, "base_path": "/drop", **kw}


def test_sftp_learns_the_host_key_then_catches_a_change(client, auth, sftp, env):
    r = client.post("/api/connections/test", json={"type": "sftp", "config": _sftp_cfg(sftp), "secrets": {"password": "secret"}}, headers=auth).json()
    assert r["ok"] and r["learned"]["host_key_sha256"].startswith("SHA256:") and "1 entry" in r["detail"], r
    cid = add_conn(client, auth, "sftp", uniq("sftp"), _sftp_cfg(sftp), {"password": "secret"})
    assert client.get(f"/api/connections/{cid}", headers=auth).json()["host"] == f"sftp://conduit@127.0.0.1:{sftp.port}/drop"
    assert client.post(f"/api/connections/{cid}/test", headers=auth).json()["ok"]
    stored = client.get(f"/api/connections/{cid}", headers=auth).json()["config"]["host_key_sha256"]
    assert stored == r["learned"]["host_key_sha256"]

    # same address, new host key: a hard error, also in the health check state
    port = sftp.port
    sftp.close()
    other = SFTPStub(env / "sftp-root", port=port)
    try:
        r = client.post(f"/api/connections/{cid}/test", headers=auth).json()
        assert not r["ok"] and "host key" in r["error"] and "changed" in r["error"]
        assert client.get(f"/api/connections/{cid}", headers=auth).json()["status"] == "err"
        # "Forget" the stored fingerprint: the next test trusts the new key
        client.patch(f"/api/connections/{cid}", json={"config": {"host_key_sha256": None}}, headers=auth)
        assert client.post(f"/api/connections/{cid}/test", headers=auth).json()["ok"]
        assert client.get(f"/api/connections/{cid}", headers=auth).json()["config"]["host_key_sha256"] != stored
    finally:
        other.close()


def test_sftp_private_key_and_bad_password(client, auth, sftp):
    key = paramiko.RSAKey.generate(2048)
    sftp.client_key = key
    import io
    buf = io.StringIO()
    key.write_private_key(buf)
    r = client.post("/api/connections/test", json={"type": "sftp", "config": _sftp_cfg(sftp), "secrets": {"private_key": buf.getvalue()}}, headers=auth).json()
    assert r["ok"], r["error"]
    r = client.post("/api/connections/test", json={"type": "sftp", "config": _sftp_cfg(sftp), "secrets": {"password": "wrong"}}, headers=auth).json()
    assert not r["ok"] and "Authentication failed" in r["error"] and "wrong" not in r["error"]


# ---- HTTP: base URL + secret headers ----------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self._serve(head=True)

    def do_GET(self):
        self._serve(head=False)

    def _serve(self, head: bool):
        if self.path.startswith("/private") and self.headers.get("Authorization") != "Bearer t0ken":
            self.send_response(401)
            self.end_headers()
            return
        body = b"id,name\n1,a\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/csv")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)


@pytest.fixture()
def web():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_http_connection_with_secret_headers(client, auth, web):
    cfg = {"base_url": f"{web}/private/?sig=abc123"}
    r = client.post("/api/connections/test", json={"type": "http", "config": cfg, "secrets": {}}, headers=auth).json()
    assert not r["ok"] and "401" in r["error"] and "abc123" not in r["error"]
    secrets = {"headers": "Authorization: Bearer t0ken\n# a comment"}
    r = client.post("/api/connections/test", json={"type": "http", "config": cfg, "secrets": secrets}, headers=auth).json()
    assert r["ok"] and "text/csv" in r["detail"], r
    cid = add_conn(client, auth, "http", uniq("web"), cfg, secrets)
    c = client.get(f"/api/connections/{cid}", headers=auth).json()
    assert c["host"] == f"{web}/private/" and "t0ken" not in str(c)
    r = client.post("/api/connections", json={"name": "bad", "type": "http", "config": {"base_url": "ftp://x"}}, headers=auth)
    assert r.status_code == 422
