"""What a viewer reads: health, status, counts and readable labels, never SQL, filters, paths, config or data rows."""
import json

import pytest

from jobhelpers import uniq, wait_job
from test_compare import _conn, _run, _side, _sqlite
from test_pipelines_api import _pipeline_body, _seed, _sqlite_conn

SECRET = "secret_marker"  # put into SQL / WHERE / paths below; a viewer's response must never contain it


@pytest.fixture()
def world(client, auth, env):
    src_path = _seed(env, f"{SECRET}.sqlite")  # the path, too, is something a viewer doesn't see
    src = _sqlite_conn(client, auth, src_path)
    dst = _sqlite_conn(client, auth, env / "dst.sqlite")
    return {"src": src, "dst": dst, "env": env}


def _names(client, auth, *ids):
    conns = {c["id"]: c["name"] for c in client.get("/api/connections", headers=auth).json()}
    return [conns[i] for i in ids]


def test_monitor_shows_a_viewer_health_not_where_or_how(client, auth, auth_viewer, world):
    for body in ({"name": "q", "kind": "query", "connection_id": world["src"], "query": f"select 1 as {SECRET}"},
                 {"name": "h", "kind": "http", "target": f"http://127.0.0.1:9/health?token={SECRET}"}):
        assert client.post("/api/monitor/checks", json=body, headers=auth).status_code == 201
    r = client.get("/api/monitor", headers=auth_viewer)
    assert r.status_code == 200 and SECRET not in r.text, r.text
    snap = r.json()
    assert snap["connections"] and all(set(c) == {"id", "name", "type", "status", "latency_ms", "last_checked_at", "note"}
                                       for c in snap["connections"])
    by = {s["name"]: s for s in snap["services"]}
    assert by["q"]["target"].endswith("· query") and by["q"]["query"] is None and by["q"]["connection_id"] is None
    assert by["h"]["target"] == "http://127.0.0.1:9/health"
    assert snap["host"]["cpu"] and "active" in snap["jobs"]
    assert SECRET in client.get("/api/monitor", headers=auth).text  # an admin sees it all


def test_a_viewers_job_detail_has_labels_not_the_spec(client, auth, auth_viewer, world):
    body = {"source": {"connection_id": world["src"], "query": f"select * from accounts /* {SECRET} */"},
            "target": {"connection_id": world["dst"], "table": "out", "mode": "append"}, "options": {"workers": 1}}
    r = client.post("/api/jobs", json=body, headers=auth)
    assert r.status_code == 201, r.text
    jid = r.json()["id"]
    assert wait_job(client, auth, jid)["status"] == "succeeded"
    src_name, dst_name = _names(client, auth, world["src"], world["dst"])

    r = client.get(f"/api/jobs/{jid}", headers=auth_viewer)
    assert r.status_code == 200 and SECRET not in r.text, r.text
    d = r.json()
    assert d["spec"] is None and set(d["plan"]) <= {"notes", "warnings"}
    assert (d["source_label"], d["target_label"]) == (f"query ({src_name})", f"out ({dst_name})")
    assert d["started_by"] == "alice" and d["log"] and d["rows_written"] == 3

    token = auth_viewer["Authorization"][7:]
    with client.stream("GET", f"/api/jobs/{jid}/events?token={token}") as s:
        text = "".join(s.iter_text())
    first = json.loads(text.split("data: ", 1)[1].split("\n", 1)[0])
    assert first["spec"] is None and SECRET not in text

    assert SECRET in client.get(f"/api/jobs/{jid}", headers=auth).json()["spec"]["source"]["query"]
    listed = client.get("/api/jobs", headers=auth_viewer)
    assert SECRET not in listed.text and listed.json()[0]["started_by"] == "alice"


def test_a_viewers_pipeline_list_has_no_steps_or_sql(client, auth, auth_viewer, world):
    steps = [{"id": "s1", "kind": "sql", "config": {"sql": f"SELECT * FROM {{{{ input }}}} WHERE acct <> '{SECRET}'"}}]
    pid = client.post("/api/pipelines", json=_pipeline_body(world["src"], world["dst"], steps=steps, name="nightly"),
                      headers=auth).json()["id"]
    r = client.get("/api/pipelines", headers=auth_viewer)
    assert r.status_code == 200 and SECRET not in r.text, r.text
    p = r.json()[0]
    assert {"source", "target", "steps", "options"}.isdisjoint(p)
    src_name, dst_name = _names(client, auth, world["src"], world["dst"])
    assert (p["source_label"], p["target_label"], p["step_count"], p["created_by"], p["runnable"]) == \
        (f"accounts ({src_name})", f"clean_accounts ({dst_name})", 1, "alice", False)
    assert client.get(f"/api/pipelines/{pid}", headers=auth_viewer).status_code == 403  # the builder's view


def test_a_viewer_sees_comparison_counts_not_their_specs(client, auth, auth_viewer, env):
    s = _conn(client, auth, "sqlite", _sqlite(env, "s.sqlite", "create table src(id integer primary key, v text)", [(1, "a"), (2, "b")]))
    t = _conn(client, auth, "sqlite", _sqlite(env, "t.sqlite", "create table tgt(id integer primary key, v text)", [(1, "a")]))
    c = _run(client, auth, {"source": _side(s, "src", where=f"v <> '{SECRET}'"), "target": _side(t, "tgt")})
    assert c["status"] == "succeeded"
    for r in (client.get("/api/compare", headers=auth_viewer), client.get(f"/api/compare/{c['id']}", headers=auth_viewer)):
        assert r.status_code == 200 and SECRET not in r.text, r.text
    v = client.get(f"/api/compare/{c['id']}", headers=auth_viewer).json()
    assert v["spec"] is None and v["summary"]["missing"] == 1 and v["source_label"].startswith("src (")
    assert client.get(f"/api/compare/{c['id']}/rows?set=missing", headers=auth_viewer).status_code == 403
    assert client.get(f"/api/compare/{c['id']}/export?sets=missing&format=csv", headers=auth_viewer).status_code == 403


def test_a_viewer_reads_no_data_rows(client, auth, auth_viewer, world):
    """Rejects, previews and dry-run samples, with real ids (the permission sweep uses random ones)."""
    body = {"source": {"connection_id": world["src"], "table": "accounts"},
            "target": {"connection_id": world["dst"], "table": uniq("out"), "mode": "append"}, "options": {"workers": 1}}
    jid = client.post("/api/jobs", json=body, headers=auth).json()["id"]
    wait_job(client, auth, jid)
    pid = client.post("/api/pipelines", json=_pipeline_body(world["src"], world["dst"]), headers=auth).json()["id"]
    for method, url, payload in (("GET", f"/api/jobs/{jid}/rejects", None),
                                 ("POST", "/api/jobs/plan", body),
                                 ("POST", f"/api/pipelines/{pid}/preview", {"sample_rows": 5}),
                                 ("POST", "/api/files/inspect", {"path": str(world["env"] / f"{SECRET}.sqlite")}),
                                 ("GET", f"/api/connections/{world['src']}/tables", None)):
        r = client.request(method, url, json=payload, headers=auth_viewer)
        assert r.status_code == 403 and SECRET not in r.text, (url, r.status_code)
