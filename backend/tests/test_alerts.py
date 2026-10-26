"""Alert rules end to end: once on break, nothing while still broken, once on recovery; thresholds, mute, the hooks
on jobs / the scheduler / comparisons, and the API. Messages go to a webhook channel pointed at a local server."""
import datetime as dt
import uuid

import pytest

from alerthelpers import Sink
from conftest import login, make_user
from jobhelpers import wait_job
from test_pipelines_api import _pipeline_body, _seed, _sqlite_conn


@pytest.fixture()
def alerting(client, auth, monkeypatch):
    from app.alerts import delivery

    monkeypatch.setattr(delivery, "INLINE", True)
    s = Sink()
    r = client.post("/api/alerts/channels", json={"name": "hook", "kind": "webhook", "secrets": {"url": f"{s.url}/h"}}, headers=auth)
    assert r.status_code == 201, r.text
    yield {"sink": s, "channel": r.json()["id"]}
    s.close()


def rule(client, auth, channel, event, **kw):
    r = client.post("/api/alerts/rules", json={"name": kw.pop("name", f"{event} rule"), "event": event,
                                               "channel_ids": [channel], **kw}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def now():
    from app.models import utcnow

    return utcnow()


def summary(msgs):
    return [(m["status"], m["title"]) for m in msgs]


# ---- connections -------------------------------------------------------------------------------------------

def _conn(name="prod-pg", env=None):
    from app.db import get_sessionmaker
    from app.models import Connection

    with get_sessionmaker()() as s:
        c = Connection(name=name, type="postgres", config={}, display_host="db", env=env)
        s.add(c)
        s.commit()
        return c.id


def _check_result(cid, status, note="connection refused"):
    from app.connectors.base import TestResult
    from app.db import get_sessionmaker
    from app.models import Connection
    from app.services import apply_result

    with get_sessionmaker()() as s:
        apply_result(s.get(Connection, cid), TestResult(ok=status != "err", latency_ms=3, degraded=status == "warn",
                                                        error=note if status == "err" else None))
        s.commit()


def test_connection_down_once_on_break_nothing_while_broken_once_on_recovery(client, auth, alerting):
    from app.alerts.engine import evaluate

    sink = alerting["sink"]
    rule(client, auth, alerting["channel"], "connection_down", options={"k": 2})
    cid = _conn()
    _check_result(cid, "err")
    evaluate()
    assert sink.take() == []  # one failed check: below K
    _check_result(cid, "err")
    evaluate()
    [m] = sink.take()
    assert (m["status"], m["title"], m["reason"]) == ("firing", "Connection down: prod-pg", "connection refused")
    assert m["subject"]["kind"] == "connection" and m["url"] is None  # no public_url: no links
    for _ in range(3):
        _check_result(cid, "err")
        evaluate()
        evaluate()  # idempotent: the same state twice sends nothing
    assert sink.take() == []
    _check_result(cid, "warn")  # a warn after err is not "back": it stays firing for an err-only rule
    evaluate()
    assert sink.take() == []
    _check_result(cid, "ok")
    evaluate()
    [m] = sink.take()
    assert m["status"] == "recovered" and m["title"] == "Recovered: prod-pg"
    evaluate()
    assert sink.take() == []


def test_flapping_below_the_threshold_never_fires(client, auth, alerting):
    from app.alerts.engine import evaluate

    rule(client, auth, alerting["channel"], "connection_down", options={"k": 2})
    cid = _conn()
    for status in ("err", "ok", "err", "ok", "err", "ok"):
        _check_result(cid, status)
        evaluate()
    assert alerting["sink"].take() == []


def test_warn_counts_when_the_rule_includes_it_and_env_scope(client, auth, alerting):
    from app.alerts.engine import evaluate

    rule(client, auth, alerting["channel"], "connection_down", options={"k": 2, "include_warn": True},
         scope={"envs": ["prod"]})
    prod, dev = _conn("prod-ch", env="prod"), _conn("dev-ch", env="dev")
    for _ in range(2):
        _check_result(prod, "warn")
        _check_result(dev, "err")
        evaluate()
    assert summary(alerting["sink"].take()) == [("firing", "Connection down: prod-ch")]


def test_query_strings_are_masked_in_messages(client, auth, alerting):
    from app.alerts.engine import evaluate

    rule(client, auth, alerting["channel"], "connection_down", options={"k": 1})
    cid = _conn()
    _check_result(cid, "err", "Could not reach https://u:pw@files.example.com/h?token=abc123 (timeout)")
    evaluate()
    [m] = alerting["sink"].take()
    assert m["reason"] == "Could not reach https://files.example.com/h (timeout)"


# ---- service checks ----------------------------------------------------------------------------------------

def test_check_down_counts_consecutive_runs(client, auth, alerting, monkeypatch):
    from app.alerts.engine import evaluate
    from app.db import get_sessionmaker
    from app.models import ServiceCheck
    from app.monitor import checks as chk

    rule(client, auth, alerting["channel"], "check_down", options={"k": 3})
    with get_sessionmaker()() as s:
        c = ServiceCheck(name="orders api", kind="http", target="http://x")
        s.add(c)
        s.commit()
        check_id = c.id
    result = {"v": ("down", "HTTP 503")}
    monkeypatch.setattr(chk, "run_check", lambda c: chk.Outcome(*result["v"], 5.0))
    for _ in range(2):
        chk.run_and_store(check_id)
        evaluate()
    assert alerting["sink"].take() == []
    chk.run_and_store(check_id)
    evaluate()
    [m] = alerting["sink"].take()
    assert (m["title"], m["reason"]) == ("Service check down: orders api", "HTTP 503")
    result["v"] = ("up", "200 OK")
    chk.run_and_store(check_id)
    evaluate()
    assert summary(alerting["sink"].take()) == [("recovered", "Recovered: orders api")]
    with get_sessionmaker()() as s:
        assert (s.get(ServiceCheck, check_id).fail_streak, s.get(ServiceCheck, check_id).fail_since) == (0, None)


# ---- host --------------------------------------------------------------------------------------------------

def test_host_threshold_must_last_m_minutes(client, auth, alerting):
    from app.alerts.engine import evaluate

    rule(client, auth, alerting["channel"], "host_high", options={"resources": ["disk"], "pct": 90, "minutes": 5})

    def snap(pct):
        return lambda: {"memory": {"percent": 99}, "cpu": {"percent": 99}, "disks": [{"label": "Data", "percent": pct}]}

    t0, sink = now(), alerting["sink"]
    evaluate(t0, snap(95))
    evaluate(t0 + dt.timedelta(minutes=3), snap(50))  # dipped: the clock starts over
    evaluate(t0 + dt.timedelta(minutes=4), snap(96))
    evaluate(t0 + dt.timedelta(minutes=8), snap(96))  # only 4 minutes above
    assert sink.take() == []
    evaluate(t0 + dt.timedelta(minutes=9), snap(97))
    [m] = sink.take()
    assert (m["title"], m["reason"]) == ("Disk Data high", "Disk Data at 97% (threshold 90%)")  # memory / cpu not picked
    evaluate(t0 + dt.timedelta(minutes=10), snap(98))
    evaluate(t0 + dt.timedelta(minutes=11), snap(40))
    assert summary(sink.take()) == [("recovered", "Recovered: Disk Data")]


# ---- jobs --------------------------------------------------------------------------------------------------

def _pipeline(client, auth, env, **kw):
    """A pipeline whose Python step fails while env/fail exists: the run fails at runtime (a launch checks only the
    source and steps' shape), and succeeds again once the flag is gone."""
    flag = env / "fail"
    code = (f"import os\ndef transform(df):\n    if os.path.exists({str(flag)!r}):\n"
            "        raise ValueError('the bank file is late')\n    return df")
    src = _sqlite_conn(client, auth, _seed(env))
    dst = _sqlite_conn(client, auth, env / "dst.sqlite")
    body = _pipeline_body(src, dst, name="nightly-orders", steps=[{"id": "py", "kind": "python", "config": {"code": code}}], **kw)
    pid = client.post("/api/pipelines", json=body, headers=auth).json()["id"]
    assert client.post(f"/api/pipelines/{pid}/preview", json={"sample_rows": 5}, headers=auth).status_code == 200
    return pid, flag


def _run(client, auth, pid):
    r = client.post(f"/api/pipelines/{pid}/run", headers=auth)
    assert r.status_code == 200, r.text
    return wait_job(client, auth, r.json()["id"])


def test_a_pipeline_failure_then_its_next_success_recovers(client, auth, alerting, env):
    sink = alerting["sink"]
    rule(client, auth, alerting["channel"], "job_failed", name="failed pipelines", options={"jobs": "pipelines"})
    rule(client, auth, alerting["channel"], "job_failed", name="failed scheduled runs", options={"jobs": "scheduled"})
    pid, flag = _pipeline(client, auth, env)
    flag.write_text("x")
    job = _run(client, auth, pid)
    assert job["status"] == "failed"
    [m] = sink.wait(1)  # the manual run is not a scheduled one: only the first rule fires
    assert (m["status"], m["title"], m["rule"]["name"]) == ("firing", "Pipeline failed: nightly-orders", "failed pipelines")
    assert "the bank file is late" in m["reason"] and m["subject"] == {"kind": "pipeline", "id": pid, "name": "nightly-orders"}
    assert _run(client, auth, pid)["status"] == "failed"  # still broken: nothing more
    flag.unlink()
    assert _run(client, auth, pid)["status"] == "succeeded"
    msgs = sink.wait(2)
    assert summary(msgs) == [("firing", "Pipeline failed: nightly-orders"), ("recovered", "Recovered: nightly-orders")]
    assert msgs[1]["since"] == msgs[0]["since"]


def test_a_failing_channel_or_a_broken_hook_never_fails_the_job(client, auth, alerting, env, monkeypatch):
    from app.alerts import engine

    dead = client.post("/api/alerts/channels", json={"name": "dead", "kind": "webhook",
                                                     "secrets": {"url": "http://127.0.0.1:9/nowhere"}}, headers=auth).json()["id"]
    rule(client, auth, dead, "job_failed")
    pid, flag = _pipeline(client, auth, env)
    flag.write_text("x")
    assert _run(client, auth, pid)["status"] == "failed"  # failed as it should, not crashed by the dead channel
    for _ in range(50):
        ch = next(x for x in client.get("/api/alerts/channels", headers=auth).json() if x["id"] == dead)
        if ch["last_error"]:
            break
        import time
        time.sleep(0.1)
    assert ch["last_error"].startswith("Webhook: ConnectError") and ch["last_sent_at"] is None

    monkeypatch.setattr(engine, "_job_finished", lambda *a: 1 / 0)  # a broken alert hook
    flag.unlink()
    assert _run(client, auth, pid)["status"] == "succeeded"


def test_a_one_off_job_failure_sends_one_message_and_keeps_no_state(client, auth, alerting):
    from app.alerts.engine import job_finished
    from app.db import get_sessionmaker
    from app.models import AlertState, Job

    rule(client, auth, alerting["channel"], "job_failed")
    with get_sessionmaker()() as s:
        j = Job(name="orders.csv → public.orders", kind="file_load", status="failed", error="bad row", finished_at=now())
        s.add(j)
        s.commit()
        jid = j.id
    job_finished(jid, "failed")
    [m] = alerting["sink"].take()
    assert (m["title"], m["reason"]) == ("Job failed: orders.csv → public.orders", "bad row")
    with get_sessionmaker()() as s:
        assert s.query(AlertState).count() == 0
    assert client.get("/api/alerts/active", headers=auth).json()["alerts"] == []


def test_a_stalled_job_fires_and_recovers_when_it_ends(client, auth, alerting):
    from app.alerts.engine import evaluate
    from app.db import get_sessionmaker
    from app.models import Job

    rule(client, auth, alerting["channel"], "job_stalled")
    with get_sessionmaker()() as s:
        j = Job(name="big load", status="running", heartbeat_at=now() - dt.timedelta(minutes=5))
        fresh = Job(name="fine load", status="running", heartbeat_at=now())
        s.add_all([j, fresh])
        s.commit()
        jid = j.id
    evaluate()
    assert summary(alerting["sink"].take()) == [("firing", "Job stalled: big load")]
    evaluate()
    assert alerting["sink"].take() == []
    with get_sessionmaker()() as s:
        s.get(Job, jid).status = "failed"
        s.commit()
    evaluate()
    assert summary(alerting["sink"].take()) == [("recovered", "Recovered: big load")]


# ---- the scheduler -----------------------------------------------------------------------------------------

def test_schedule_couldnt_start_then_starts(client, auth, alerting, env):
    from app.db import get_sessionmaker
    from app.engine.schedule import run_due
    from app.models import Pipeline

    rule(client, auth, alerting["channel"], "schedule_failed")
    src = _sqlite_conn(client, auth, _seed(env))
    dst = _sqlite_conn(client, auth, env / "dst.sqlite")
    pid = client.post("/api/pipelines", json=_pipeline_body(src, dst, name="hourly", schedule="0 * * * *"), headers=auth).json()["id"]

    def overdue():
        with get_sessionmaker()() as s:
            s.get(Pipeline, uuid.UUID(pid)).next_run_at = now() - dt.timedelta(minutes=1)
            s.commit()

    overdue()
    assert run_due() == 0  # no cached preview: it can't start
    [m] = alerting["sink"].take()
    assert m["title"] == "Schedule couldn't start: hourly" and "no cached preview" in m["reason"]
    run_due()
    assert alerting["sink"].take() == []  # still the same problem: nothing more
    assert client.post(f"/api/pipelines/{pid}/preview", json={"sample_rows": 5}, headers=auth).status_code == 200
    overdue()
    assert run_due() == 1
    assert summary(alerting["sink"].wait(1)) == [("recovered", "Recovered: hourly")]


# ---- stale remote input ------------------------------------------------------------------------------------

def test_pipeline_input_stale_when_no_new_file_for_n_hours(client, auth, alerting):
    from app.alerts.engine import evaluate
    from app.db import get_sessionmaker
    from app.models import Pipeline, RemoteLoad

    rule(client, auth, alerting["channel"], "input_stale", options={"hours": 24})
    sftp, dst = str(uuid.uuid4()), str(uuid.uuid4())
    with get_sessionmaker()() as s:
        s.add(Pipeline(name="daily drop", description="", enabled=True, schedule="0 6 * * *", options={}, steps=[],
                       source={"remote": {"connection_id": sftp, "path": "in/orders_*.csv", "match": "newest", "skip_loaded": True}},
                       target={"connection_id": dst, "table": "orders", "mode": "append"}))
        s.add(RemoteLoad(source_key=f"remote:{sftp}", target_key=f"{dst}:orders", path="sftp://h/in/orders_1.csv",
                         path_hash="a", version="1", loaded_at=now() - dt.timedelta(hours=30)))
        s.commit()
    evaluate()
    [m] = alerting["sink"].take()
    assert m["title"] == "No new input file: daily drop" and "more than 24 h ago" in m["reason"]
    with get_sessionmaker()() as s:
        s.add(RemoteLoad(source_key=f"remote:{sftp}", target_key=f"{dst}:orders", path="sftp://h/in/orders_2.csv",
                         path_hash="b", version="1", loaded_at=now()))
        s.commit()
    evaluate()
    assert summary(alerting["sink"].take()) == [("recovered", "Recovered: daily drop")]


# ---- comparisons -------------------------------------------------------------------------------------------

def test_a_comparison_with_differences_sends_one_message(client, auth, alerting, env):
    from test_compare import _conn as cmp_conn, _run as cmp_run, _side, _sqlite

    rule(client, auth, alerting["channel"], "compare_diff")
    s = cmp_conn(client, auth, "sqlite", _sqlite(env, "s.sqlite", "create table src(id integer primary key, v text)", [(1, "a"), (2, "b")]))
    t = cmp_conn(client, auth, "sqlite", _sqlite(env, "t.sqlite", "create table tgt(id integer primary key, v text)", [(1, "x")]))
    c = cmp_run(client, auth, {"source": _side(s, "src"), "target": _side(t, "tgt")})
    [m] = alerting["sink"].wait(1)
    assert m["title"] == f"Compare found differences: {c['name']}" and m["subject"]["kind"] == "comparison"


# ---- mute --------------------------------------------------------------------------------------------------

def test_mute_sends_nothing_then_one_summary_of_what_still_fires(client, auth, alerting):
    from app.alerts.engine import evaluate

    sink = alerting["sink"]
    rule(client, auth, alerting["channel"], "connection_down", options={"k": 1})
    a, b = _conn("prod-pg"), _conn("prod-ch")
    _check_result(b, "err")
    evaluate()
    assert summary(sink.take()) == [("firing", "Connection down: prod-ch")]  # notified before the mute
    until = now() + dt.timedelta(hours=1)
    assert client.put("/api/alerts/mute", json={"until": until.isoformat()}, headers=auth).status_code == 200
    _check_result(a, "err")  # breaks while muted
    _check_result(b, "ok")  # recovers while muted
    evaluate()
    assert sink.take() == []
    active = client.get("/api/alerts/active", headers=auth).json()
    assert active["muted_until"] and [x["subject_label"] for x in active["alerts"]] == ["prod-pg"]
    evaluate(until + dt.timedelta(seconds=1))  # the mute ran out
    [m] = sink.take()
    assert m["status"] == "summary" and m["title"] == "Alerts unmuted: 1 still firing, 1 recovered meanwhile"
    assert [(i["title"], i["status"]) for i in m["items"]] == [("Connection down: prod-ch", "recovered"),
                                                                ("Connection down: prod-pg", "firing")]
    _check_result(a, "ok")
    evaluate(until + dt.timedelta(seconds=40))
    assert summary(sink.take()) == [("recovered", "Recovered: prod-pg")]  # its break went out in the summary
    assert client.get("/api/alerts/mute", headers=auth).json() == {"until": None}


def test_unmuting_by_hand_sends_the_summary_now(client, auth, alerting):
    from app.alerts.engine import evaluate

    rule(client, auth, alerting["channel"], "connection_down", options={"k": 1})
    a = _conn()
    client.put("/api/alerts/mute", json={"until": (now() + dt.timedelta(hours=4)).isoformat()}, headers=auth)
    _check_result(a, "err")
    evaluate()
    assert alerting["sink"].take() == []
    assert client.put("/api/alerts/mute", json={"until": None}, headers=auth).json() == {"until": None}
    [m] = alerting["sink"].take()
    assert m["title"] == "Alerts unmuted: 1 still firing"
    r = client.put("/api/alerts/mute", json={"until": (now() + dt.timedelta(days=8)).isoformat()}, headers=auth)
    assert r.status_code == 422


# ---- the API -----------------------------------------------------------------------------------------------

def test_channel_secrets_are_never_returned_and_blank_keeps_them(client, auth, alerting):
    sink = alerting["sink"]
    r = client.post("/api/alerts/channels", json={"name": "tg", "kind": "telegram", "config": {"chat_id": "-100"},
                                                  "secrets": {"bot_token": "123:SECRET"}}, headers=auth)
    assert r.status_code == 201 and "SECRET" not in r.text and r.json()["secrets_set"] == ["bot_token"]
    cid = r.json()["id"]
    r = client.patch(f"/api/alerts/channels/{cid}", json={"config": {"chat_id": "-200"}, "secrets": {"bot_token": ""}}, headers=auth)
    assert r.status_code == 200 and r.json()["config"] == {"chat_id": "-200"} and r.json()["secrets_set"] == ["bot_token"]
    assert client.post("/api/alerts/channels", json={"name": "tg", "kind": "slack", "secrets": {"webhook_url": "https://x"}},
                       headers=auth).status_code == 409
    hook = alerting["channel"]
    r = client.patch(f"/api/alerts/channels/{hook}", json={"secrets": {"signing_secret": "k"}}, headers=auth)
    assert r.json()["secrets_set"] == ["signing_secret", "url"]
    r = client.patch(f"/api/alerts/channels/{hook}", json={"clear_secrets": ["signing_secret"]}, headers=auth)
    assert r.json()["secrets_set"] == ["url"]
    # Test: a real message, the real error
    r = client.post(f"/api/alerts/channels/{hook}/test", headers=auth)
    assert r.json() == {"channel_id": hook, "name": "hook", "ok": True, "error": None}
    assert sink.take()[0]["status"] == "test"
    sink.reply = (500, b"boom")
    r = client.post(f"/api/alerts/channels/{hook}/test", headers=auth).json()
    assert r["ok"] is False and r["error"] == "HTTP 500 boom"
    assert next(c for c in client.get("/api/alerts/channels", headers=auth).json() if c["id"] == hook)["last_error"] == "HTTP 500 boom"
    sink.reply = (200, b"{}")
    sink.take()
    r = client.post("/api/alerts/test", json={"channel_ids": [hook], "rule_name": "Failed scheduled pipelines"}, headers=auth)
    assert [x["ok"] for x in r.json()] == [True] and sink.take()[0]["title"].endswith(": Failed scheduled pipelines")
    r = client.post("/api/alerts/channels/test", json={"id": hook, "name": "hook", "kind": "webhook", "secrets": {}}, headers=auth)
    assert r.json()["ok"] is True  # an edit's test with blank secrets uses the saved ones


@pytest.mark.parametrize("body, says", [
    ({"event": "connection_down", "options": {"k": 0}}, "between 1 and 20"),
    ({"event": "host_high", "options": {"resources": ["gpu"]}}, "disk, memory, cpu"),
    ({"event": "job_failed", "options": {"jobs": "some"}}, "scheduled, pipelines, all"),
    ({"event": "input_stale", "options": {"hours": "x"}}, "must be a number"),
    ({"event": "nope"}, ""),
])
def test_rules_are_validated(client, auth, alerting, body, says):
    r = client.post("/api/alerts/rules", json={"name": "r", "channel_ids": [alerting["channel"]], **body}, headers=auth)
    assert r.status_code == 422 and says in r.text, r.text


def test_rule_defaults_scope_and_unknown_channels(client, auth, alerting):
    r = rule(client, auth, alerting["channel"], "connection_down", scope={"connections": [], "checks": ["x"]})
    assert r["scope"] == {"all": True} and r["options"] == {"k": 2, "include_warn": False}
    assert client.post("/api/alerts/rules", json={"name": "x", "event": "check_down", "channel_ids": [str(uuid.uuid4())]},
                       headers=auth).status_code == 422
    assert client.post("/api/alerts/rules", json={"name": "connection_down rule", "event": "check_down"},
                       headers=auth).status_code == 409
    rid = r["id"]
    assert client.delete(f"/api/alerts/channels/{alerting['channel']}", headers=auth).status_code == 204
    assert client.get("/api/alerts/rules", headers=auth).json()[0]["channel_ids"] == []  # the rule stays, sending nowhere
    assert client.delete(f"/api/alerts/rules/{rid}", headers=auth).status_code == 204


def test_active_alerts_for_viewers_have_no_ids(client, auth, alerting):
    from app.alerts.engine import evaluate

    rule(client, auth, alerting["channel"], "connection_down", options={"k": 1})
    _check_result(_conn(), "err")
    evaluate()
    admin = client.get("/api/alerts/active", headers=auth).json()["alerts"][0]
    assert admin["rule_id"] and admin["subject_id"] and admin["link"] == "#/monitor"
    make_user("vee", "viewer")
    viewer = client.get("/api/alerts/active", headers=login(client, "vee")).json()["alerts"][0]
    assert viewer["rule_id"] is None and viewer["subject_id"] is None
    assert (viewer["subject_label"], viewer["reason"], viewer["event_label"]) == ("prod-pg", "connection refused", "Connection down")
