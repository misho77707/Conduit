import sqlite3

import duckdb


def _sqlite_file(dir_, name="src.sqlite", rows=5):
    p = dir_ / name
    con = sqlite3.connect(p)
    con.execute("create table events(id integer primary key, payload text)")
    con.executemany("insert into events(payload) values (?)", [(f"e{i}",) for i in range(rows)])
    con.commit()
    con.close()
    return p


def _duck_file(dir_, name="wh.duckdb"):
    p = dir_ / name
    con = duckdb.connect(str(p))
    con.execute("create table t as select range as id from range(1000)")
    con.close()
    return p


def test_db_types_metadata(client, auth):
    types = {t["type"]: t for t in client.get("/api/meta/db-types", headers=auth).json()}
    assert set(types) == {"postgres", "clickhouse", "mysql", "sqlite", "duckdb", "mongodb"}
    assert types["mongodb"]["kind"] == "uri" and types["sqlite"]["kind"] == "file"
    assert types["postgres"]["default_port"] == 5432


def test_create_list_get_patch_delete(client, auth, env):
    p = _sqlite_file(env)
    body = {"name": "scratch-sqlite", "type": "sqlite", "env": "local", "config": {"path": str(p)}}
    r = client.post("/api/connections", json=body, headers=auth)
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["status"] == "idle" and c["host"] == str(p) and c["env"] == "local"

    assert [x["name"] for x in client.get("/api/connections", headers=auth).json()] == ["scratch-sqlite"]
    assert client.get(f"/api/connections/{c['id']}", headers=auth).json()["name"] == "scratch-sqlite"

    r = client.patch(f"/api/connections/{c['id']}", json={"name": "renamed", "env": "staging"}, headers=auth)
    assert r.status_code == 200 and r.json()["name"] == "renamed" and r.json()["env"] == "staging"

    assert client.delete(f"/api/connections/{c['id']}", headers=auth).status_code == 204
    assert client.get(f"/api/connections/{c['id']}", headers=auth).status_code == 404


def test_duplicate_name_is_409(client, auth, env):
    p = _sqlite_file(env)
    body = {"name": "dup", "type": "sqlite", "config": {"path": str(p)}}
    assert client.post("/api/connections", json=body, headers=auth).status_code == 201
    assert client.post("/api/connections", json=body, headers=auth).status_code == 409


def test_unknown_type_and_missing_fields_are_422(client, auth):
    r = client.post("/api/connections", json={"name": "x", "type": "oracle"}, headers=auth)
    assert r.status_code == 422
    r = client.post("/api/connections", json={"name": "x", "type": "postgres", "config": {}}, headers=auth)
    assert r.status_code == 422 and "Host is required" in r.text
    r = client.post("/api/connections", json={"name": "x", "type": "mongodb"}, headers=auth)
    assert r.status_code == 422 and "Connection string" in r.text


def test_secrets_are_encrypted_at_rest_and_never_returned(client, auth):
    body = {"name": "pg", "type": "postgres", "config": {"host": "db.internal", "port": 5432, "user": "etl", "database": "w"},
            "secrets": {"password": "hunter2-very-secret"}}
    r = client.post("/api/connections", json=body, headers=auth)
    assert r.status_code == 201
    cid = r.json()["id"]
    for resp in (r, client.get("/api/connections", headers=auth), client.get(f"/api/connections/{cid}", headers=auth)):
        assert "hunter2-very-secret" not in resp.text

    from sqlalchemy import select
    from app.db import get_sessionmaker
    from app.models import Connection

    with get_sessionmaker()() as s:
        row = s.scalar(select(Connection))
        assert "hunter2-very-secret" not in row.secret_enc and row.secret_enc
        assert "hunter2-very-secret" not in str(row.config)


def test_patch_keeps_existing_password_when_omitted(client, auth):
    body = {"name": "pg", "type": "postgres", "config": {"host": "h", "user": "u"}, "secrets": {"password": "keepme-123"}}
    cid = client.post("/api/connections", json=body, headers=auth).json()["id"]
    r = client.patch(f"/api/connections/{cid}", json={"config": {"host": "h2"}}, headers=auth)
    assert r.status_code == 200 and r.json()["host"] == "h2" and r.json()["status"] == "idle"

    from app.db import get_sessionmaker
    from app.models import Connection
    from app.security import decrypt_secrets

    with get_sessionmaker()() as s:
        assert decrypt_secrets(s.get(Connection, __import__("uuid").UUID(cid)).secret_enc) == {"password": "keepme-123"}


def test_mongo_uri_is_masked_in_display(client, auth):
    body = {"name": "m", "type": "mongodb", "config": {"database": "events"},
            "secrets": {"uri": "mongodb+srv://etl:TopSecretPw@cluster0.abc.mongodb.net/?retryWrites=true"}}
    r = client.post("/api/connections", json=body, headers=auth)
    assert r.status_code == 201
    assert r.json()["host"] == "mongodb+srv://cluster0.abc.mongodb.net"
    assert "TopSecretPw" not in r.text


def test_test_saved_sqlite_updates_state(client, auth, env):
    p = _sqlite_file(env)
    cid = client.post("/api/connections", json={"name": "s", "type": "sqlite", "config": {"path": str(p)}}, headers=auth).json()["id"]
    r = client.post(f"/api/connections/{cid}/test", headers=auth)
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] and j["status"] == "ok" and j["version"].startswith("SQLite") and "1 tables" in j["detail"]
    assert j["connection"]["status"] == "ok" and j["connection"]["last_checked_at"]
    assert j["connection"]["server_version"].startswith("SQLite")


def test_test_saved_failure_records_error_note(client, auth, env):
    p = _sqlite_file(env)
    cid = client.post("/api/connections", json={"name": "s", "type": "sqlite", "config": {"path": str(p)}}, headers=auth).json()["id"]
    p.unlink()
    j = client.post(f"/api/connections/{cid}/test", headers=auth).json()
    assert not j["ok"] and j["status"] == "err" and "does not exist" in j["error"]
    assert j["connection"]["status"] == "err" and "does not exist" in j["connection"]["note"]


def test_test_unsaved_duckdb(client, auth, env):
    p = _duck_file(env)
    r = client.post("/api/connections/test", json={"type": "duckdb", "config": {"path": str(p), "read_only": True}}, headers=auth)
    assert r.status_code == 200 and r.json()["ok"] and r.json()["version"].startswith("DuckDB")
    assert client.get("/api/connections", headers=auth).json() == []  # nothing stored


def test_file_paths_outside_allowed_roots_are_rejected(client, auth):
    for path in ("/etc/passwd", "../../etc/passwd"):
        r = client.post("/api/connections/test", json={"type": "sqlite", "config": {"path": path}}, headers=auth)
        assert r.status_code == 422 and "allowed folder" in r.text


def test_create_if_missing_makes_a_new_duckdb(client, auth, env):
    p = env / "new" / "fresh.duckdb"
    r = client.post("/api/connections/test", json={"type": "duckdb", "config": {"path": str(p), "create_if_missing": True}}, headers=auth)
    assert r.json()["ok"] and p.exists()
    r = client.post("/api/connections/test", json={"type": "duckdb", "config": {"path": str(env / "nope.duckdb")}}, headers=auth)
    assert not r.json()["ok"]


def test_list_tables(client, auth, env):
    sp, dp = _sqlite_file(env, rows=7), _duck_file(env)
    a = client.post("/api/connections", json={"name": "a", "type": "sqlite", "config": {"path": str(sp)}}, headers=auth).json()["id"]
    b = client.post("/api/connections", json={"name": "b", "type": "duckdb", "config": {"path": str(dp)}}, headers=auth).json()["id"]
    ta = client.get(f"/api/connections/{a}/tables", headers=auth).json()
    assert ta == [{"name": "main.events", "est_rows": 7, "size_bytes": ta[0]["size_bytes"], "exact": True, "info": ""}]
    tb = client.get(f"/api/connections/{b}/tables", headers=auth).json()
    assert tb[0]["name"] == "main.t" and tb[0]["est_rows"] == 1000


def test_inspect_table_sqlite(client, auth, env):
    p = env / "s.sqlite"
    con = sqlite3.connect(p)
    con.execute("create table t(id integer primary key, ts timestamp)")
    con.execute("create index idx_ts on t(ts)")
    con.commit()
    con.close()
    cid = client.post("/api/connections", json={"name": "s", "type": "sqlite", "config": {"path": str(p)}}, headers=auth).json()["id"]

    r = client.get(f"/api/connections/{cid}/tables/main.t/inspect", headers=auth)
    assert r.status_code == 200
    j = r.json()
    assert j["exact"] is True
    verdicts = {c["column"]: c["verdict"] for c in j["candidates"]}
    assert verdicts["id"] == "recommended" and verdicts["ts"] == "alternative"


def test_inspect_table_missing_connection_is_404(client, auth):
    import uuid
    r = client.get(f"/api/connections/{uuid.uuid4()}/tables/x/inspect", headers=auth)
    assert r.status_code == 404


def test_file_inspect_endpoint(client, auth, env):
    p = env / "f.csv"
    p.write_text("a,b\n1,2\n3,4\n")
    r = client.post("/api/files/inspect", json={"path": str(p)}, headers=auth)
    assert r.status_code == 200
    j = r.json()
    assert j["format"] == "csv" and j["row_estimate"] == 2 and j["exact"]
    assert j["preview"] == [{"a": 1, "b": 2}, {"a": 3, "b": 4}]


def test_file_inspect_rejects_path_outside_roots(client, auth):
    r = client.post("/api/files/inspect", json={"path": "/etc/passwd"}, headers=auth)
    assert r.status_code == 422


def test_file_inspect_unsupported_extension(client, auth, env):
    p = env / "f.exe"
    p.write_bytes(b"x")
    r = client.post("/api/files/inspect", json={"path": str(p)}, headers=auth)
    assert r.status_code == 422


def test_file_inspect_missing_file(client, auth, env):
    r = client.post("/api/files/inspect", json={"path": str(env / "missing.csv")}, headers=auth)
    assert r.status_code == 404


def test_file_list_endpoint(client, auth, env):
    (env / "sub").mkdir()
    p = env / "a.csv"
    p.write_bytes(b"a\n1\n")
    r = client.get("/api/files/list", headers=auth)
    assert r.status_code == 200
    names = {e["name"]: e for e in r.json()}
    assert names["sub"]["is_dir"] is True and names["sub"]["format"] is None
    assert names["a.csv"]["is_dir"] is False and names["a.csv"]["format"] == "csv"
    assert names["a.csv"]["size_bytes"] == p.stat().st_size


def test_file_list_rejects_path_outside_roots(client, auth):
    r = client.get("/api/files/list?path=/etc", headers=auth)
    assert r.status_code == 422


def test_file_list_missing_dir_is_404(client, auth, env):
    r = client.get(f"/api/files/list?path={env / 'nope'}", headers=auth)
    assert r.status_code == 404


def test_unreachable_server_fails_fast_without_leaking_password(client, auth):
    body = {"name": "dead", "type": "postgres",
            "config": {"host": "127.0.0.1", "port": 1, "user": "u", "database": "d"}, "secrets": {"password": "leak-check-pw"}}
    cid = client.post("/api/connections", json=body, headers=auth).json()["id"]
    j = client.post(f"/api/connections/{cid}/test", headers=auth).json()
    assert not j["ok"] and j["status"] == "err"
    assert "leak-check-pw" not in str(j)


def test_test_all_runs_every_connection(client, auth, env):
    sp = _sqlite_file(env)
    client.post("/api/connections", json={"name": "good", "type": "sqlite", "config": {"path": str(sp)}}, headers=auth)
    client.post("/api/connections", json={"name": "bad", "type": "duckdb", "config": {"path": str(env / "missing.duckdb")}}, headers=auth)
    res = client.post("/api/connections/test-all", headers=auth).json()
    assert {r["connection"]["name"]: r["status"] for r in res} == {"good": "ok", "bad": "err"}


def test_health_check_task_updates_all_connections(client, auth, env):
    sp = _sqlite_file(env)
    client.post("/api/connections", json={"name": "good", "type": "sqlite", "config": {"path": str(sp)}}, headers=auth)
    from app.worker.tasks import check_all_connections

    assert check_all_connections() == {"ok": 1}
    assert client.get("/api/connections", headers=auth).json()[0]["status"] == "ok"
