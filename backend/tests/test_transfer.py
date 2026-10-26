"""Export / import (Control & Setup): the file, the dry run, apply, secrets under a passphrase, and the safety rules."""
import json
import sqlite3

import pytest

from conftest import login, make_user

PASS = "a long passphrase 42"


def _sqlite(client, auth, path, name):
    r = client.post("/api/connections", json={"name": name, "type": "sqlite", "config": {"path": str(path), "create_if_missing": True}}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _seed_instance(client, auth, env):
    """One of everything: connections (one with a password), a query check, a scheduled pipeline, channels, a rule, a setting."""
    src = env / "src.sqlite"
    c = sqlite3.connect(src)
    c.execute("create table accounts(acct text, amt real)")
    c.executemany("insert into accounts values (?,?)", [("a1", 100.0), ("a2", -5.0)])
    c.commit()
    c.close()
    ids = {"src": _sqlite(client, auth, src, "src-db"), "dst": _sqlite(client, auth, env / "dst.sqlite", "dst-db")}
    r = client.post("/api/connections", json={"name": "prod-pg", "type": "postgres", "env": "prod",
                                              "config": {"host": "127.0.0.1", "port": 1, "database": "sales", "user": "loader"},
                                              "secrets": {"password": "hunter2-very-secret"}}, headers=auth)
    assert r.status_code == 201, r.text
    ids["pg"] = r.json()["id"]
    r = client.post("/api/monitor/checks", json={"name": "rows exist", "kind": "query", "connection_id": ids["src"],
                                                 "query": "select 1", "expect": "1", "interval_s": 60, "enabled": False}, headers=auth)
    assert r.status_code in (200, 201), r.text
    ids["check"] = r.json()["id"]
    r = client.post("/api/pipelines", json={
        "name": "nightly", "description": "positive amounts", "schedule": "0 2 * * *", "enabled": True,
        "source": {"connection_id": ids["src"], "table": "accounts"},
        "target": {"connection_id": ids["dst"], "table": "clean", "mode": "append"},
        "options": {"workers": 1}, "steps": [{"id": "s1", "kind": "filter", "config": {"expr": "amt > 0"}}]}, headers=auth)
    assert r.status_code == 201, r.text
    ids["pipe"] = r.json()["id"]
    r = client.post("/api/alerts/channels", json={"name": "hook", "kind": "webhook", "config": {},
                                                  "secrets": {"url": "https://hooks.example.com/x?token=abc", "signing_secret": "sign-me"}}, headers=auth)
    assert r.status_code == 201, r.text
    ids["hook"] = r.json()["id"]
    r = client.post("/api/alerts/channels", json={"name": "mail", "kind": "email",
                                                  "config": {"host": "smtp.example.com", "tls": "starttls", "from": "c@example.com", "to": ["t@example.com"]}}, headers=auth)
    assert r.status_code == 201, r.text
    r = client.post("/api/alerts/rules", json={"name": "nightly fails", "event": "job_failed", "options": {"jobs": "scheduled"},
                                               "scope": {"pipelines": [ids["pipe"]]}, "channel_ids": [ids["hook"]]}, headers=auth)
    assert r.status_code == 201, r.text
    r = client.post("/api/alerts/rules", json={"name": "db down", "event": "connection_down",
                                               "scope": {"connections": [ids["pg"], ids["src"]]}, "channel_ids": [ids["hook"]]}, headers=auth)
    assert r.status_code == 201, r.text
    assert client.put("/api/settings/keep_finished_jobs", json={"value": 25}, headers=auth).status_code == 200
    return ids


def _export(client, auth, **body):
    r = client.post("/api/export", json=body, headers=auth)
    assert r.status_code == 200, r.text
    assert r.headers["content-disposition"].startswith("attachment; filename=")
    return r.json()


def _second_instance(monkeypatch, tmp_path):
    """A fresh, empty app database behind the same client: another Conduit."""
    from app.config import get_settings
    from app.db import Base, get_engine, reset_engine_cache

    get_engine().dispose()
    monkeypatch.setenv("CONDUIT_DATABASE_URL", f"sqlite:///{tmp_path / 'second.db'}")
    get_settings.cache_clear()
    reset_engine_cache()
    Base.metadata.create_all(get_engine())
    make_user("admin", "superadmin")
    make_user("bob", "admin")


def _plan(client, auth, doc, passphrase=None):
    r = client.post("/api/import/plan", json={"document": doc, "passphrase": passphrase}, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def _items(plan):
    out = {}
    for i in plan["items"]:
        out.setdefault((i["section"], i["name"]), i)  # a name twice in the file: the first one
    return out


def _apply(client, auth, doc, picked, passphrase=None):
    r = client.post("/api/import/apply", json={"document": doc, "passphrase": passphrase, "picked": picked}, headers=auth)
    assert r.status_code == 200, r.text
    return {(x["section"], x["name"]): x for x in r.json()}


def test_the_export_file_has_names_not_ids_and_no_secrets(client, auth, env):
    ids = _seed_instance(client, auth, env)
    doc = _export(client, auth)
    raw = json.dumps(doc)
    assert (doc["format"], doc["version"], doc["instance"], doc["secrets"]) == ("conduit-export", 1, "Conduit", "none")
    assert doc["exported_at"].endswith("Z") and "encryption" not in doc
    for secret in ("hunter2-very-secret", "sign-me", "token=abc"):
        assert secret not in raw
    for i in ids.values():
        assert i not in raw  # every reference is a name
    pg = next(c for c in doc["connections"] if c["name"] == "prod-pg")
    assert pg["secrets_set"] == ["password"] and "secrets" not in pg and pg["env"] == "prod"
    p = doc["pipelines"][0]
    assert p["source"]["connection"] == "src-db" and p["target"]["connection"] == "dst-db" and "connection_id" not in p["source"]
    assert (p["schedule"], p["enabled"], p["steps"][0]["kind"]) == ("0 2 * * *", True, "filter")
    assert doc["service_checks"][0]["connection"] == "src-db"
    rules = {r["name"]: r for r in doc["alert_rules"]}
    assert rules["nightly fails"]["scope"] == {"pipelines": ["nightly"]} and rules["nightly fails"]["channels"] == ["hook"]
    assert rules["db down"]["scope"] == {"connections": ["prod-pg", "src-db"]}
    assert doc["settings"] == {"keep_finished_jobs": 25}
    for leftover in ("users", "jobs", "comparisons", "watermarks"):
        assert leftover not in doc


def test_round_trip_into_an_empty_instance_without_secrets(client, auth, env, monkeypatch, tmp_path):
    _seed_instance(client, auth, env)
    doc = _export(client, auth)
    _second_instance(monkeypatch, tmp_path)
    auth2 = login(client, "bob")

    plan = _plan(client, auth2, doc)
    items = _items(plan)
    assert plan["instance"] == "Conduit" and plan["secrets_used"] is False
    assert any("never imported" in n for n in plan["notes"]) and any("no secrets" in n for n in plan["notes"])
    assert {i["action"] for i in plan["items"]} == {"create"}
    assert "Needs its password" in items[("connections", "prod-pg")]["notes"][0]
    assert "Arrives disabled, schedule kept (0 2 * * *)" in items[("pipelines", "nightly")]["notes"][0]
    assert "arrives disabled" in " ".join(items[("alert_channels", "hook")]["notes"])

    res = _apply(client, auth2, doc, [i["key"] for i in plan["items"]])
    assert all(r["ok"] for r in res.values()), res

    conns = {c["name"]: c for c in client.get("/api/connections", headers=auth2).json()}
    assert conns["prod-pg"]["needs_credentials"] is True and not conns["src-db"]["needs_credentials"]
    assert conns["prod-pg"]["config"]["host"] == "127.0.0.1" and conns["prod-pg"]["env"] == "prod"
    p = next(x for x in client.get("/api/pipelines", headers=auth2).json() if x["name"] == "nightly")
    full = client.get(f"/api/pipelines/{p['id']}", headers=auth2).json()
    assert (full["enabled"], full["schedule"], full["next_run_at"], full["output_columns"]) == (False, "0 2 * * *", None, None)
    assert full["source"]["connection_id"] == conns["src-db"]["id"] and full["target"]["connection_id"] == conns["dst-db"]["id"]
    r = client.post(f"/api/pipelines/{p['id']}/run", headers=auth2)
    assert r.status_code == 422 and "preview" in r.json()["detail"]  # no cached preview came with it
    chans = {c["name"]: c for c in client.get("/api/alerts/channels", headers=auth2).json()}
    assert chans["hook"]["enabled"] is False and "Needs its signing secret and URL" in chans["hook"]["last_error"]
    assert chans["mail"]["enabled"] is True and chans["mail"]["config"]["host"] == "smtp.example.com"
    rules = {r["name"]: r for r in client.get("/api/alerts/rules", headers=auth2).json()}
    assert rules["nightly fails"]["scope"] == {"pipelines": [p["id"]]} and rules["nightly fails"]["channel_ids"] == [chans["hook"]["id"]]
    s = next(x for x in client.get("/api/settings", headers=auth2).json() if x["key"] == "keep_finished_jobs")
    assert (s["value"], s["source"], s["updated_by"]) == (25, "ui", "bob")
    checks = client.get("/api/monitor/checks", headers=auth2).json()
    assert checks[0]["name"] == "rows exist" and checks[0]["connection_id"] == conns["src-db"]["id"]

    # the same file again: nothing to do
    again = _plan(client, auth2, doc)["items"]
    assert {i["action"] for i in again} == {"skip"}, [i for i in again if i["action"] != "skip"]


def test_a_connection_without_its_secrets_is_unusable_until_edited(client, auth, env, monkeypatch, tmp_path):
    _seed_instance(client, auth, env)
    doc = _export(client, auth)
    _second_instance(monkeypatch, tmp_path)
    auth2 = login(client, "bob")
    _apply(client, auth2, doc, [i["key"] for i in _plan(client, auth2, doc)["items"]])
    pg = next(c for c in client.get("/api/connections", headers=auth2).json() if c["name"] == "prod-pg")

    r = client.post(f"/api/connections/{pg['id']}/test", headers=auth2).json()
    assert r["ok"] is False and "Needs credentials" in r["error"]
    assert client.get(f"/api/connections/{pg['id']}/tables", headers=auth2).status_code == 422
    from app.db import get_sessionmaker
    from app.models import Connection
    from app.services import all_connections, test_many

    with get_sessionmaker()() as s:  # the beat's health check: skipped, never "down"
        test_many(s, all_connections(s))
        c = s.get(Connection, __import__("uuid").UUID(pg["id"]))
        assert (c.status, c.err_streak, c.note) == ("idle", 0, "Needs credentials: edit it and add its secrets")
    r = client.post("/api/jobs", json={"source": {"connection_id": pg["id"], "table": "public.x"},
                                       "target": {"connection_id": pg["id"], "table": "public.y"}}, headers=auth2)
    assert r.status_code == 422 and "needs credentials" in r.json()["detail"]

    r = client.patch(f"/api/connections/{pg['id']}", json={"config": {"port": 2}}, headers=auth2)
    assert r.json()["needs_credentials"] is True  # edited without secrets: still unusable
    r = client.patch(f"/api/connections/{pg['id']}", json={"secrets": {"password": "the-new-one"}}, headers=auth2)
    assert r.json()["needs_credentials"] is False
    r = client.post(f"/api/connections/{pg['id']}/test", headers=auth2).json()
    assert "Needs credentials" not in (r["error"] or "")  # now it really tries (and fails on port 2)


def test_secrets_round_trip_under_a_passphrase(client, auth_superadmin, env, monkeypatch, tmp_path):
    _seed_instance(client, auth_superadmin, env)
    doc = _export(client, auth_superadmin, include_secrets=True, passphrase=PASS)
    raw = json.dumps(doc)
    assert doc["secrets"] == "encrypted" and doc["encryption"]["kdf"] == "scrypt" and doc["encryption"]["n"] == 32768
    for secret in ("hunter2-very-secret", "sign-me", "token=abc"):
        assert secret not in raw
    _second_instance(monkeypatch, tmp_path)
    auth2 = login(client, "bob")

    r = client.post("/api/import/plan", json={"document": doc, "passphrase": "not the passphrase"}, headers=auth2)
    assert r.status_code == 422 and "Wrong passphrase" in r.json()["detail"]
    plan = _plan(client, auth2, doc)  # no passphrase: secrets left out, said so
    assert plan["secrets_used"] is False and any("no passphrase" in n for n in plan["notes"])
    plan = _plan(client, auth2, doc, PASS)
    assert plan["secrets_used"] is True and "Secrets included" in _items(plan)[("connections", "prod-pg")]["notes"]
    res = _apply(client, auth2, doc, [i["key"] for i in plan["items"]], PASS)
    assert all(r["ok"] for r in res.values()), res

    from app.db import get_sessionmaker
    from app.models import AlertChannel, Connection
    from app.security import decrypt_secrets
    from sqlalchemy import select

    with get_sessionmaker()() as s:  # re-encrypted with this instance's key
        pg = s.scalar(select(Connection).where(Connection.name == "prod-pg"))
        assert decrypt_secrets(pg.secret_enc) == {"password": "hunter2-very-secret"} and pg.needs_credentials is False
        hook = s.scalar(select(AlertChannel).where(AlertChannel.name == "hook"))
        assert decrypt_secrets(hook.secret_enc)["signing_secret"] == "sign-me" and hook.enabled is True


def test_only_a_superadmin_exports_secrets_and_the_passphrase_is_checked(client, auth, auth_superadmin, auth_viewer):
    r = client.post("/api/export", json={"include_secrets": True, "passphrase": PASS}, headers=auth)
    assert r.status_code == 403 and "superadmin" in r.json()["detail"]
    assert client.post("/api/export", json={}, headers=auth).status_code == 200
    r = client.post("/api/export", json={"include_secrets": True}, headers=auth_superadmin)
    assert r.status_code == 422 and "passphrase" in r.json()["detail"]
    r = client.post("/api/export", json={"include_secrets": True, "passphrase": "short"}, headers=auth_superadmin)
    assert r.status_code == 422 and "at least 12" in r.json()["detail"]
    for path, body in (("/api/export", {}), ("/api/import/plan", {"document": {}}), ("/api/import/apply", {"document": {}, "picked": []})):
        assert client.post(path, json=body, headers=auth_viewer).status_code == 403


def test_files_this_conduit_cannot_read_are_refused(client, auth):
    for doc, why in (({"hello": 1}, "Not a Conduit export"),
                     ({"format": "conduit-export"}, "no format version"),
                     ({"format": "conduit-export", "version": 2}, "newer Conduit")):
        r = client.post("/api/import/plan", json={"document": doc}, headers=auth)
        assert r.status_code == 422 and why in r.json()["detail"], r.text


def test_conflicts_missing_references_and_partial_apply(client, auth, env):
    _sqlite(client, auth, env / "here.sqlite", "src-db")  # here, "src-db" is sqlite ...
    doc = {"format": "conduit-export", "version": 1, "instance": "Other", "secrets": "none",
           "settings": {"upload_max_mb": 999999, "nope": 1, "remote_max_files": 50},
           "connections": [
               {"name": "src-db", "type": "duckdb", "config": {"path": str(env / "x.duckdb")}, "secrets_set": []},  # ... not duckdb
               {"name": "new-db", "type": "sqlite", "config": {"path": str(env / "new.sqlite"), "create_if_missing": True}, "secrets_set": []},
               {"name": "new-db", "type": "sqlite", "config": {"path": str(env / "dup.sqlite")}, "secrets_set": []}],
           "pipelines": [
               {"name": "uses-ghost", "source": {"connection": "ghost", "table": "t"}, "target": {"connection": "new-db", "table": "t"}},
               {"name": "uses-new", "source": {"connection": "new-db", "table": "t"}, "target": {"connection": "new-db", "table": "u"}},
               {"name": "bad-cron", "schedule": "every day", "source": {"connection": "new-db", "table": "t"}, "target": {"connection": "new-db", "table": "u"}}],
           "alert_rules": [
               {"name": "half there", "event": "connection_down", "scope": {"connections": ["new-db", "ghost"]}, "channels": ["nowhere"]},
               {"name": "none there", "event": "connection_down", "scope": {"connections": ["ghost"]}},
               {"name": "odd", "event": "no_such_event"}]}
    items = _items(_plan(client, auth, doc))
    assert items[("connections", "src-db")]["action"] == "conflict" and "Exists here as a sqlite" in items[("connections", "src-db")]["notes"][0]
    assert items[("connections", "new-db")]["action"] == "create"
    assert sum(1 for i in _plan(client, auth, doc)["items"] if i["name"] == "new-db" and "twice" in " ".join(i["notes"])) == 1
    assert items[("pipelines", "uses-ghost")]["action"] == "conflict" and "connection 'ghost'" in items[("pipelines", "uses-ghost")]["notes"][0]
    assert items[("pipelines", "uses-new")]["action"] == "create"
    assert items[("pipelines", "bad-cron")]["action"] == "conflict"
    assert items[("settings", "Upload limit")]["action"] == "conflict" and "between 1 and" in items[("settings", "Upload limit")]["notes"][0]
    assert items[("settings", "nope")]["action"] == "conflict"
    half = items[("alert_rules", "half there")]
    assert half["action"] == "create" and "connection 'ghost'" in " ".join(half["notes"]) and "'nowhere'" in " ".join(half["notes"])
    assert items[("alert_rules", "none there")]["action"] == "conflict"  # never widened to "every connection"
    assert items[("alert_rules", "odd")]["action"] == "conflict"

    plan = _plan(client, auth, doc)
    key = {k: i["key"] for k, i in _items(plan).items()}
    # the pipeline is picked without the connection it needs (which is not here yet): it alone fails
    res = _apply(client, auth, doc, [key[("pipelines", "uses-new")], key[("settings", "Files per run, at most")],
                                     key[("connections", "src-db")]])
    assert res[("pipelines", "uses-new")]["ok"] is False and "pick it too" in res[("pipelines", "uses-new")]["error"]
    assert res[("settings", "Files per run, at most")]["ok"] is True
    assert res[("connections", "src-db")]["ok"] is False and "Can't be applied" in res[("connections", "src-db")]["error"]
    res = _apply(client, auth, doc, [key[("connections", "new-db")], key[("pipelines", "uses-new")], key[("alert_rules", "half there")]])
    assert all(r["ok"] for r in res.values()), res
    rule = next(r for r in client.get("/api/alerts/rules", headers=auth).json() if r["name"] == "half there")
    new_db = next(c for c in client.get("/api/connections", headers=auth).json() if c["name"] == "new-db")
    assert rule["scope"] == {"connections": [new_db["id"]]} and rule["channel_ids"] == []


def test_an_update_disables_the_pipeline_and_drops_its_cached_preview(client, auth, env):
    ids = _seed_instance(client, auth, env)
    r = client.post(f"/api/pipelines/{ids['pipe']}/preview", json={"sample_rows": 10}, headers=auth)
    assert r.status_code == 200, r.text
    assert client.get(f"/api/pipelines/{ids['pipe']}", headers=auth).json()["output_columns"]
    doc = _export(client, auth)
    doc["pipelines"][0]["steps"][0]["config"]["expr"] = "amt > 10"
    item = _items(_plan(client, auth, doc))[("pipelines", "nightly")]
    assert item["action"] == "update" and item["notes"][0] == "Changes its steps" and "Arrives disabled" in item["notes"][1]
    assert _apply(client, auth, doc, [item["key"]])[("pipelines", "nightly")]["ok"]
    p = client.get(f"/api/pipelines/{ids['pipe']}", headers=auth).json()
    assert (p["enabled"], p["next_run_at"], p["output_columns"], p["schedule"]) == (False, None, None, "0 2 * * *")
    assert p["steps"][0]["config"]["expr"] == "amt > 10"


@pytest.mark.parametrize("version", [1])
def test_an_import_keeps_existing_secrets_on_update(client, auth, env, version):
    ids = _seed_instance(client, auth, env)
    doc = _export(client, auth)
    doc["connections"] = [c for c in doc["connections"] if c["name"] == "prod-pg"]
    doc["connections"][0]["config"]["port"] = 6543
    item = _items(_plan(client, auth, doc))[("connections", "prod-pg")]
    assert item["action"] == "update" and item["notes"] == ["Changes its settings", "Keeps the secrets saved here"]
    assert _apply(client, auth, doc, [item["key"]])[("connections", "prod-pg")]["ok"]
    from app.db import get_sessionmaker
    from app.models import Connection
    from app.security import decrypt_secrets
    import uuid

    with get_sessionmaker()() as s:
        c = s.get(Connection, uuid.UUID(ids["pg"]))
        assert c.config["port"] == 6543 and decrypt_secrets(c.secret_enc) == {"password": "hunter2-very-secret"}
        assert c.needs_credentials is False
