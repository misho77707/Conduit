"""Monitor: service checks (HTTP / TCP / query / docker), host resources and the snapshot. Uses real local sockets,
servers, and (for the docker kind) a real Docker daemon when one is available."""
import datetime as dt
import http.server
import os
import socket
import sqlite3
import subprocess
import threading
import time
import uuid

import pytest

from jobhelpers import add_conn, needs_docker, uniq


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/ok":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"fine")
        elif self.path == "/redir":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
        else:
            self.send_response(500)
            self.end_headers()

    def log_message(self, *a):  # keep test output quiet
        pass


@pytest.fixture()
def web():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture()
def listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(5)
    yield s.getsockname()[1]
    s.close()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _make(client, auth, **body):
    body.setdefault("name", uniq("chk"))
    r = client.post("/api/monitor/checks", json=body, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def _settled(client, auth, check_id, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        c = next(x for x in client.get("/api/monitor/checks", headers=auth).json() if x["id"] == check_id)
        if c["status"] != "pending":
            return c
        time.sleep(0.2)
    raise AssertionError("check never produced a result")


# ---- HTTP -----------------------------------------------------------------------------------------------------

def test_http_check_up_down_and_redirect_is_compared_not_followed(client, auth, web):
    ok = _settled(client, auth, _make(client, auth, kind="http", target=f"{web}/ok")["id"])
    assert ok["status"] == "up" and ok["message"] == "200 OK" and ok["latency_ms"] is not None and ok["checked_at"]
    bad = _settled(client, auth, _make(client, auth, kind="http", target=f"{web}/broken")["id"])
    assert bad["status"] == "down" and bad["message"] == "500 Internal Server Error, expected 200"
    redir = _settled(client, auth, _make(client, auth, kind="http", target=f"{web}/redir", expect="302")["id"])
    assert redir["status"] == "up" and redir["message"].startswith("302")
    gone = _settled(client, auth, _make(client, auth, kind="http", target=f"http://127.0.0.1:{_free_port()}/")["id"])
    assert gone["status"] == "down" and gone["message"] == "connection refused"


# ---- TCP ------------------------------------------------------------------------------------------------------

def test_tcp_check_open_and_closed_port(client, auth, listener):
    up = _settled(client, auth, _make(client, auth, kind="tcp", target=f"127.0.0.1:{listener}")["id"])
    assert up["status"] == "up" and up["message"] == "port open"
    down = _settled(client, auth, _make(client, auth, kind="tcp", target=f"127.0.0.1:{_free_port()}")["id"])
    assert down["status"] == "down" and down["message"] == "connection refused"


def test_tcp_check_slower_than_the_limit_is_a_warning(monkeypatch, client, auth, listener):
    import app.monitor.checks as chk

    real = socket.create_connection
    monkeypatch.setattr(chk.socket, "create_connection", lambda *a, **k: (time.sleep(0.05), real(*a, **k))[1])
    c = _settled(client, auth, _make(client, auth, kind="tcp", target=f"127.0.0.1:{listener}", expect="5")["id"])
    assert c["status"] == "warn" and "slow" in c["message"]


# ---- query ----------------------------------------------------------------------------------------------------

def _sqlite_conn(client, auth, env):
    p = env / "m.sqlite"
    c = sqlite3.connect(p)
    c.execute("create table t(id integer)")
    c.executemany("insert into t values (?)", [(1,), (2,), (3,)])
    c.commit()
    c.close()
    return add_conn(client, auth, "sqlite", uniq("sq"), {"path": str(p), "create_if_missing": True})


def test_query_check_counts_rows_and_reports_errors(client, auth, env):
    conn = _sqlite_conn(client, auth, env)
    ok = _settled(client, auth, _make(client, auth, kind="query", connection_id=conn, query="SELECT 1", expect="1")["id"])
    assert ok["status"] == "up" and ok["message"] == "1 row"
    assert ok["target"].endswith("· SELECT 1")
    three = _settled(client, auth, _make(client, auth, kind="query", connection_id=conn, query="select id from t;", expect="2")["id"])
    assert three["status"] == "down" and three["message"] == "3 rows, expected 2"
    any_rows = _settled(client, auth, _make(client, auth, kind="query", connection_id=conn, query="select id from t")["id"])
    assert any_rows["status"] == "up" and any_rows["message"] == "3 rows"
    broken = _settled(client, auth, _make(client, auth, kind="query", connection_id=conn, query="select nope from t")["id"])
    assert broken["status"] == "down" and "nope" in broken["message"].lower()


def test_query_check_survives_a_deleted_connection(client, auth, env):
    conn = _sqlite_conn(client, auth, env)
    c = _make(client, auth, kind="query", connection_id=conn, query="SELECT 1")
    _settled(client, auth, c["id"])
    assert client.delete(f"/api/connections/{conn}", headers=auth).status_code == 204
    r = client.post(f"/api/monitor/checks/{c['id']}/run", headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "down" and "deleted" in r.json()["message"]


# ---- docker -----------------------------------------------------------------------------------------------------

# A tiny image to run for these tests. Override with CONDUIT_TEST_DOCKER_IMAGE for an offline/registry-restricted
# environment (e.g. a locally `docker import`-ed busybox rootfs) - it just needs a "sleep" and a "true" on the PATH.
_TEST_IMAGE = os.environ.get("CONDUIT_TEST_DOCKER_IMAGE", "alpine:3")


def _docker(*args: str) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True, timeout=20).stdout.strip()


@pytest.fixture()
def container():
    """A plain running container, no HEALTHCHECK."""
    name = uniq("mon")
    _docker("run", "-d", "--name", name, _TEST_IMAGE, "sleep", "300")
    yield name
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=20)


@pytest.fixture()
def healthy_container():
    """A container with a HEALTHCHECK that passes almost immediately."""
    name = uniq("monh")
    _docker("run", "-d", "--name", name, "--health-cmd", "true", "--health-interval", "1s",
            "--health-retries", "1", "--health-start-period", "0s", _TEST_IMAGE, "sleep", "300")
    yield name
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=20)


def test_docker_check_reports_a_clear_error_when_the_socket_is_missing(client, auth, monkeypatch, tmp_path):
    from app.config import get_settings

    monkeypatch.setenv("CONDUIT_DOCKER_SOCKET", str(tmp_path / "no-such.sock"))
    get_settings.cache_clear()
    down = _settled(client, auth, _make(client, auth, kind="docker", target="anything")["id"])
    assert down["status"] == "down" and "socket not found" in down["message"]


@needs_docker
def test_docker_check_missing_container_is_down(client, auth):
    down = _settled(client, auth, _make(client, auth, kind="docker", target=uniq("nope"))["id"])
    assert down["status"] == "down" and down["message"] == "container not found"


@needs_docker
def test_docker_check_running_container_is_up_and_flips_down_when_stopped(client, auth, container):
    up = _settled(client, auth, _make(client, auth, kind="docker", target=container)["id"])
    assert up["status"] == "up" and up["message"] == "running" and up["latency_ms"] is not None
    _docker("stop", "-t", "1", container)
    r = client.post(f"/api/monitor/checks/{up['id']}/run", headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "down" and "status:" in r.json()["message"]


@needs_docker
def test_docker_check_healthy_expect_needs_a_real_passing_healthcheck(client, auth, container, healthy_container):
    no_hc = _settled(client, auth, _make(client, auth, kind="docker", target=container, expect="healthy")["id"])
    assert no_hc["status"] == "down" and "no healthcheck" in no_hc["message"]

    check_id = _make(client, auth, kind="docker", target=healthy_container, expect="healthy")["id"]
    result = _settled(client, auth, check_id)
    end = time.time() + 20
    while result["status"] != "up" and time.time() < end:
        time.sleep(1)
        result = client.post(f"/api/monitor/checks/{check_id}/run", headers=auth).json()
    assert result["status"] == "up" and result["message"] == "healthy"


# ---- validation -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("body,fragment", [
    ({"kind": "http", "target": "ftp://x"}, "http://"),
    ({"kind": "http", "target": "http://x", "expect": "abc"}, "whole number"),
    ({"kind": "http", "target": "http://x", "expect": "999"}, "between 100 and 599"),
    ({"kind": "tcp", "target": "no-port"}, "host:port"),
    ({"kind": "tcp", "target": "h:99999"}, "host:port"),
    ({"kind": "http", "target": "http://x", "interval_s": 3}, "greater than or equal"),
    ({"kind": "query", "query": "select 1"}, "connection"),
    ({"kind": "docker", "target": ""}, "container name or ID"),
    ({"kind": "docker", "target": "c1", "expect": "sideways"}, "left blank"),
])
def test_bad_checks_are_refused(client, auth, body, fragment):
    r = client.post("/api/monitor/checks", json={"name": "x", **body}, headers=auth)
    assert r.status_code == 422 and fragment in r.text, r.text


def test_query_checks_only_read(client, auth, env):
    conn = _sqlite_conn(client, auth, env)
    for q, frag in [("delete from t", "Only SELECT"), ("select 1; drop table t", "single statement"), ("", "Enter a query")]:
        r = client.post("/api/monitor/checks", json={"name": "x", "kind": "query", "connection_id": conn, "query": q}, headers=auth)
        assert r.status_code == 422 and frag in r.text, (q, r.text)
    r = client.post("/api/monitor/checks", json={"name": "x", "kind": "query", "connection_id": str(uuid.uuid4()), "query": "select 1"}, headers=auth)
    assert r.status_code == 422 and "Connection not found" in r.text
    mg = add_conn(client, auth, "mongodb", uniq("mg"), {"database": "d"}, {"uri": "mongodb://localhost/d"})
    r = client.post("/api/monitor/checks", json={"name": "x", "kind": "query", "connection_id": mg, "query": "select 1"}, headers=auth)
    assert r.status_code == 422 and "SQL database" in r.text


# ---- scheduling, claims, editing ------------------------------------------------------------------------------

def test_run_due_respects_intervals_and_claims(client, auth, web):
    import app.monitor.checks as chk
    from app.db import get_sessionmaker
    from app.models import ServiceCheck

    c = _make(client, auth, kind="http", target=f"{web}/ok", interval_s=10, enabled=False)  # disabled: nothing runs by itself
    assert chk.run_due() == 0
    client.patch(f"/api/monitor/checks/{c['id']}", json={"enabled": True}, headers=auth)
    _settled(client, auth, c["id"])
    assert chk.run_due() == 0  # just checked, 10 s have not passed
    with get_sessionmaker()() as s:  # pretend the last run was 11 s ago
        row = s.get(ServiceCheck, uuid.UUID(c["id"]))
        row.checked_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=11)
        s.commit()
    assert chk.run_due() == 1
    with get_sessionmaker()() as s:  # another runner holds it: nobody else runs it, the API says so
        row = s.get(ServiceCheck, uuid.UUID(c["id"]))
        row.claimed_at = dt.datetime.now(dt.timezone.utc)
        s.commit()
    assert chk.run_and_store(uuid.UUID(c["id"])) is False
    assert client.post(f"/api/monitor/checks/{c['id']}/run", headers=auth).status_code == 409


def test_editing_a_check_resets_its_result_and_reruns_it(client, auth, web):
    c = _make(client, auth, kind="http", target=f"{web}/broken")
    assert _settled(client, auth, c["id"])["status"] == "down"
    r = client.patch(f"/api/monitor/checks/{c['id']}", json={"target": f"{web}/ok", "name": "renamed"}, headers=auth)
    assert r.status_code == 200 and r.json()["name"] == "renamed" and r.json()["status"] in ("pending", "up")
    assert _settled(client, auth, c["id"])["status"] == "up"
    assert client.patch(f"/api/monitor/checks/{c['id']}", json={"interval_s": 1}, headers=auth).status_code == 422
    assert client.delete(f"/api/monitor/checks/{c['id']}", headers=auth).status_code == 204
    assert client.delete(f"/api/monitor/checks/{c['id']}", headers=auth).status_code == 404


def test_check_that_stops_being_run_is_flagged_stale(client, auth, web):
    from app.db import get_sessionmaker
    from app.models import ServiceCheck

    c = _make(client, auth, kind="http", target=f"{web}/ok", interval_s=10)
    _settled(client, auth, c["id"])
    assert next(x for x in client.get("/api/monitor/checks", headers=auth).json() if x["id"] == c["id"])["stale"] is False
    with get_sessionmaker()() as s:
        row = s.get(ServiceCheck, uuid.UUID(c["id"]))
        row.checked_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
        s.commit()
    assert next(x for x in client.get("/api/monitor/checks", headers=auth).json() if x["id"] == c["id"])["stale"] is True


# ---- snapshot -------------------------------------------------------------------------------------------------

def test_snapshot_has_everything_the_screen_shows(client, auth, env, web):
    from app.db import get_sessionmaker
    from app.models import Job

    conn = _sqlite_conn(client, auth, env)
    client.post(f"/api/connections/{conn}/test", headers=auth)
    _settled(client, auth, _make(client, auth, kind="http", target=f"{web}/ok")["id"])
    _settled(client, auth, _make(client, auth, kind="http", target=f"{web}/broken")["id"])
    with get_sessionmaker()() as s:
        s.add(Job(name="a → b", kind="transfer", status="running", spec={}, heartbeat_at=dt.datetime.now(dt.timezone.utc)))
        s.add(Job(name="c → d", kind="transfer", status="queued", spec={}))
        s.add(Job(name="old", kind="transfer", status="succeeded", spec={}))
        s.commit()
    snap = client.get("/api/monitor", headers=auth).json()
    assert {"updated_at", "connections", "host", "jobs", "services", "summary"} <= set(snap)
    assert snap["summary"]["connections"]["up"] == 1 and snap["connections"][0]["latency_ms"] is not None
    assert snap["summary"]["services"] == {"up": 1, "warn": 0, "down": 1, "pending": 0, "total": 2}
    assert snap["jobs"]["running"] == 1 and snap["jobs"]["queued"] == 1 and snap["jobs"]["active"][0]["name"] == "a → b"
    assert snap["jobs"]["slots_total"] is None  # jobs run in-process in tests: unknowable
    h = snap["host"]
    assert h["vcpus"] >= 1 and 0 <= h["cpu"]["percent"] <= 100 and h["memory"]["total_gb"] > 0
    assert h["memory"]["level"] in ("ok", "high") and any(d["label"] == "Data" for d in h["disks"])


def test_extra_disks_and_thresholds_come_from_settings(client, auth, env, monkeypatch, tmp_path):
    from app.config import get_settings

    monkeypatch.setenv("CONDUIT_MONITOR_DISKS", f"{tmp_path}:Scratch,/definitely/not/here:Ghost")
    monkeypatch.setenv("CONDUIT_MONITOR_WARN_PCT", "1")  # everything is "high"
    get_settings.cache_clear()
    h = client.get("/api/monitor", headers=auth).json()["host"]
    assert [d["label"] for d in h["disks"]] == ["Data", "Scratch"] and h["disks_unavailable"] == ["/definitely/not/here"]
    assert h["warn_at_pct"] == 1 and all(d["level"] == "high" for d in h["disks"])


def test_refresh_runs_everything_now(client, auth, env, web):
    conn = _sqlite_conn(client, auth, env)
    c = _make(client, auth, kind="http", target=f"{web}/ok", interval_s=3600)
    _settled(client, auth, c["id"])
    first = next(x for x in client.get("/api/monitor/checks", headers=auth).json() if x["id"] == c["id"])["checked_at"]
    time.sleep(1.1)
    snap = client.post("/api/monitor/refresh", headers=auth).json()
    svc = next(x for x in snap["services"] if x["id"] == c["id"])
    assert svc["checked_at"] > first  # ran again although its interval is an hour
    assert next(x for x in snap["connections"] if x["id"] == conn)["status"] == "ok"


def test_monitor_needs_login(client):
    assert client.get("/api/monitor").status_code == 401
    assert client.post("/api/monitor/refresh").status_code == 401
    assert client.get("/api/monitor/checks").status_code == 401
