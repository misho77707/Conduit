"""Pipelines API: CRUD, preview (schema caching) and run, end to end against SQLite so no live server is needed."""
import datetime as dt
import sqlite3
import uuid

from jobhelpers import add_conn, add_pg, needs_pg, pg_run, uniq, wait_job


def _sqlite_conn(client, auth, path, **extra):
    return add_conn(client, auth, "sqlite", uniq("sqlite"), {"path": str(path), "create_if_missing": True, **extra})


def _seed(env, name="src.sqlite"):
    p = env / name
    c = sqlite3.connect(p)
    c.execute("create table accounts(acct text, amt real, currency text)")
    c.executemany("insert into accounts values (?,?,?)",
                  [("a1", 100.0, "USD"), ("a2", -5.0, "EUR"), ("a3", 250.0, "USD")])
    c.commit()
    c.close()
    return p


def _pipeline_body(src_id, dst_id, steps=None, **kw):
    return {
        "name": kw.pop("name", uniq("pipe")),
        "description": "test pipeline",
        "source": {"connection_id": src_id, "table": "accounts"},
        "target": {"connection_id": dst_id, "table": "clean_accounts", "mode": "append"},
        "options": {"workers": 1},
        "steps": steps or [],
        **kw,
    }


def test_create_get_list_pipeline(client, auth, env):
    src = _sqlite_conn(client, auth, _seed(env))
    dst = _sqlite_conn(client, auth, env / "dst.sqlite")
    r = client.post("/api/pipelines", json=_pipeline_body(src, dst), headers=auth)
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["output_columns"] is None and p["last_run"] is None

    r = client.get(f"/api/pipelines/{p['id']}", headers=auth)
    assert r.status_code == 200 and r.json()["name"] == p["name"]

    r = client.get("/api/pipelines", headers=auth)
    assert r.status_code == 200
    assert any(x["id"] == p["id"] for x in r.json())


def test_create_rejects_split_column_with_steps(client, auth, env):
    src = _sqlite_conn(client, auth, _seed(env))
    dst = _sqlite_conn(client, auth, env / "dst.sqlite")
    body = _pipeline_body(src, dst, steps=[{"id": "s1", "kind": "filter", "config": {"expr": "amt > 0"}}],
                          options={"split_column": "acct", "workers": 2})
    r = client.post("/api/pipelines", json=body, headers=auth)
    assert r.status_code == 422, r.text
    assert "split column" in r.text.lower()


def test_preview_infers_schema_and_caches_it(client, auth, env):
    src = _sqlite_conn(client, auth, _seed(env))
    dst = _sqlite_conn(client, auth, env / "dst.sqlite")
    steps = [
        {"id": "s1", "kind": "rename", "config": {"mapping": {"acct": "account_id"}}},
        {"id": "s2", "kind": "filter", "config": {"expr": "amt > 0"}},
        {"id": "s3", "kind": "derive", "config": {"column": "amt_usd", "expr": "amt * CASE currency WHEN 'EUR' THEN 1.1 ELSE 1 END"}},
    ]
    r = client.post("/api/pipelines", json=_pipeline_body(src, dst, steps=steps), headers=auth)
    pid = r.json()["id"]

    r = client.post(f"/api/pipelines/{pid}/preview", json={"sample_rows": 50}, headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["full_chain"] is True
    names = [c["name"] for c in body["columns"]]
    assert names == ["account_id", "amt", "currency", "amt_usd"]
    assert body["sampled_rows"] == 2  # one row (a2/EUR) is filtered out by amt > 0
    assert {round(r["amt_usd"], 2) for r in body["rows"]} == {100.0, 250.0}

    # the full-chain preview cached the output schema so the pipeline can now be run
    r = client.get(f"/api/pipelines/{pid}", headers=auth)
    assert [c["name"] for c in r.json()["output_columns"]] == ["account_id", "amt", "currency", "amt_usd"]


def test_preview_with_an_unfinished_step_keeps_the_last_good_table(client, auth, env):
    src = _sqlite_conn(client, auth, _seed(env))
    dst = _sqlite_conn(client, auth, env / "dst.sqlite")
    steps = [
        {"id": "s1", "kind": "rename", "config": {"mapping": {"acct": "account_id"}}},
        {"id": "s2", "kind": "derive", "config": {"column": "", "expr": ""}},  # just added, not filled in yet
        {"id": "s3", "kind": "filter", "config": {"expr": "amt > 0"}},
    ]
    pid = client.post("/api/pipelines", json=_pipeline_body(src, dst, steps=steps), headers=auth).json()["id"]

    r = client.post(f"/api/pipelines/{pid}/preview", json={"sample_rows": 50}, headers=auth)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["error_step"] == "s2" and "Derive step needs" in body["error"]
    assert [c["name"] for c in body["columns"]] == ["account_id", "amt", "currency"]  # output of s1
    assert body["sampled_rows"] == 3 and body["full_chain"] is False
    assert client.get(f"/api/pipelines/{pid}", headers=auth).json()["output_columns"] is None  # nothing cached
    assert client.post(f"/api/pipelines/{pid}/run", headers=auth).status_code == 422


def test_preview_upto_step_does_not_cache(client, auth, env):
    src = _sqlite_conn(client, auth, _seed(env))
    dst = _sqlite_conn(client, auth, env / "dst.sqlite")
    steps = [
        {"id": "s1", "kind": "rename", "config": {"mapping": {"acct": "account_id"}}},
        {"id": "s2", "kind": "derive", "config": {"column": "flag", "expr": "amt > 0"}},
    ]
    r = client.post("/api/pipelines", json=_pipeline_body(src, dst, steps=steps), headers=auth)
    pid = r.json()["id"]

    r = client.post(f"/api/pipelines/{pid}/preview", json={"sample_rows": 50, "upto_step": "s1"}, headers=auth)
    assert r.status_code == 200
    assert r.json()["full_chain"] is False
    assert [c["name"] for c in r.json()["columns"]] == ["account_id", "amt", "currency"]

    r = client.get(f"/api/pipelines/{pid}", headers=auth)
    assert r.json()["output_columns"] is None  # a partial preview never updates the cache


def test_run_without_preview_is_rejected(client, auth, env):
    src = _sqlite_conn(client, auth, _seed(env))
    dst = _sqlite_conn(client, auth, env / "dst.sqlite")
    r = client.post("/api/pipelines", json=_pipeline_body(src, dst, steps=[{"id": "s1", "kind": "filter", "config": {"expr": "amt > 0"}}]),
                    headers=auth)
    pid = r.json()["id"]
    r = client.post(f"/api/pipelines/{pid}/run", headers=auth)
    assert r.status_code == 422 and "preview" in r.text.lower()


def test_run_writes_transformed_rows(client, auth, env):
    src = _sqlite_conn(client, auth, _seed(env))
    dst_path = env / "dst.sqlite"
    dst = _sqlite_conn(client, auth, dst_path)
    steps = [
        {"id": "s1", "kind": "rename", "config": {"mapping": {"acct": "account_id"}}},
        {"id": "s2", "kind": "filter", "config": {"expr": "amt > 0"}},
        {"id": "s3", "kind": "derive", "config": {"column": "amt_usd", "expr": "amt * CASE currency WHEN 'EUR' THEN 1.1 ELSE 1 END"}},
    ]
    r = client.post("/api/pipelines", json=_pipeline_body(src, dst, steps=steps), headers=auth)
    pid = r.json()["id"]
    assert client.post(f"/api/pipelines/{pid}/preview", json={"sample_rows": 50}, headers=auth).status_code == 200

    r = client.post(f"/api/pipelines/{pid}/run", headers=auth)
    assert r.status_code == 200, r.text
    job = wait_job(client, auth, r.json()["id"])
    assert job["status"] == "succeeded", job
    assert job["rows_written"] == 2

    c = sqlite3.connect(dst_path)
    rows = c.execute("select account_id, currency, amt_usd from clean_accounts order by account_id").fetchall()
    c.close()
    assert rows == [("a1", "USD", 100.0), ("a3", "USD", 250.0)]

    r = client.get(f"/api/pipelines/{pid}", headers=auth)
    last = r.json()["last_run"]
    assert last["job_id"] == job["id"] and last["status"] == "succeeded" and last["rows_written"] == 2


def test_update_clears_cached_schema(client, auth, env):
    src = _sqlite_conn(client, auth, _seed(env))
    dst = _sqlite_conn(client, auth, env / "dst.sqlite")
    steps = [{"id": "s1", "kind": "filter", "config": {"expr": "amt > 0"}}]
    r = client.post("/api/pipelines", json=_pipeline_body(src, dst, steps=steps), headers=auth)
    pid = r.json()["id"]
    assert client.post(f"/api/pipelines/{pid}/preview", json={"sample_rows": 50}, headers=auth).status_code == 200
    assert client.get(f"/api/pipelines/{pid}", headers=auth).json()["output_columns"] is not None

    steps[0]["config"]["expr"] = "amt > 1000"  # a real edit: nothing will match any more
    r = client.patch(f"/api/pipelines/{pid}", json={"steps": steps}, headers=auth)
    assert r.status_code == 200
    assert r.json()["output_columns"] is None

    r = client.post(f"/api/pipelines/{pid}/run", headers=auth)
    assert r.status_code == 422 and "preview" in r.text.lower()


def test_delete_pipeline(client, auth, env):
    src = _sqlite_conn(client, auth, _seed(env))
    dst = _sqlite_conn(client, auth, env / "dst.sqlite")
    r = client.post("/api/pipelines", json=_pipeline_body(src, dst), headers=auth)
    pid = r.json()["id"]
    assert client.delete(f"/api/pipelines/{pid}", headers=auth).status_code == 204
    assert client.get(f"/api/pipelines/{pid}", headers=auth).status_code == 404


def test_python_step_end_to_end(client, auth, env):
    src = _sqlite_conn(client, auth, _seed(env))
    dst_path = env / "dst.sqlite"
    dst = _sqlite_conn(client, auth, dst_path)
    code = "def transform(df):\n    df['acct'] = df['acct'].str.upper()\n    return df\n"
    steps = [{"id": "s1", "kind": "python", "config": {"code": code}}]
    r = client.post("/api/pipelines", json=_pipeline_body(src, dst, steps=steps), headers=auth)
    pid = r.json()["id"]
    assert client.post(f"/api/pipelines/{pid}/preview", json={"sample_rows": 50}, headers=auth).status_code == 200

    r = client.post(f"/api/pipelines/{pid}/run", headers=auth)
    job = wait_job(client, auth, r.json()["id"])
    assert job["status"] == "succeeded" and job["rows_written"] == 3

    c = sqlite3.connect(dst_path)
    accts = {row[0] for row in c.execute("select acct from clean_accounts").fetchall()}
    c.close()
    assert accts == {"A1", "A2", "A3"}


# ---- scheduling -------------------------------------------------------------------------------------------

def test_create_with_a_schedule_computes_next_run_at(client, auth, env):
    src = _sqlite_conn(client, auth, _seed(env))
    dst = _sqlite_conn(client, auth, env / "dst.sqlite")
    r = client.post("/api/pipelines", json=_pipeline_body(src, dst, schedule="0 2 * * *"), headers=auth)
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["schedule"] == "0 2 * * *"
    assert p["next_run_at"] is not None

    r = client.post("/api/pipelines", json=_pipeline_body(src, dst), headers=auth)  # no schedule at all
    assert r.json()["next_run_at"] is None


def test_create_rejects_a_schedule_that_is_not_a_cron_expression(client, auth, env):
    src = _sqlite_conn(client, auth, _seed(env))
    dst = _sqlite_conn(client, auth, env / "dst.sqlite")
    r = client.post("/api/pipelines", json=_pipeline_body(src, dst, schedule="whenever"), headers=auth)
    assert r.status_code == 422 and "cron" in r.text.lower()


def test_disabling_a_pipeline_clears_next_run_at_and_enabling_recomputes_it(client, auth, env):
    src = _sqlite_conn(client, auth, _seed(env))
    dst = _sqlite_conn(client, auth, env / "dst.sqlite")
    r = client.post("/api/pipelines", json=_pipeline_body(src, dst, schedule="@hourly"), headers=auth)
    pid = r.json()["id"]
    assert r.json()["next_run_at"] is not None

    r = client.patch(f"/api/pipelines/{pid}", json={"enabled": False}, headers=auth)
    assert r.status_code == 200 and r.json()["next_run_at"] is None

    r = client.patch(f"/api/pipelines/{pid}", json={"enabled": True}, headers=auth)
    assert r.json()["next_run_at"] is not None


def test_patching_schedule_to_null_clears_it(client, auth, env):
    src = _sqlite_conn(client, auth, _seed(env))
    dst = _sqlite_conn(client, auth, env / "dst.sqlite")
    r = client.post("/api/pipelines", json=_pipeline_body(src, dst, schedule="@daily"), headers=auth)
    pid = r.json()["id"]

    r = client.patch(f"/api/pipelines/{pid}", json={"schedule": None}, headers=auth)
    assert r.status_code == 200
    assert r.json()["schedule"] is None and r.json()["next_run_at"] is None


def test_run_due_queues_and_runs_an_overdue_scheduled_pipeline(client, auth, env):
    """The actual beat-driven path: an overdue, enabled, previewed pipeline gets a real job queued and run."""
    from app.db import get_sessionmaker
    from app.engine.schedule import run_due
    from app.models import Pipeline, utcnow

    src = _sqlite_conn(client, auth, _seed(env))
    dst_path = env / "dst.sqlite"
    dst = _sqlite_conn(client, auth, dst_path)
    steps = [{"id": "s1", "kind": "filter", "config": {"expr": "amt > 0"}}]
    r = client.post("/api/pipelines", json=_pipeline_body(src, dst, steps=steps, schedule="@daily"), headers=auth)
    pid = r.json()["id"]
    assert client.post(f"/api/pipelines/{pid}/preview", json={"sample_rows": 50}, headers=auth).status_code == 200

    with get_sessionmaker()() as s:  # pretend its last scheduled slot already came and went
        row = s.get(Pipeline, uuid.UUID(pid))
        row.next_run_at = utcnow() - dt.timedelta(minutes=1)
        s.commit()

    assert run_due() == 1

    with get_sessionmaker()() as s:
        row = s.get(Pipeline, uuid.UUID(pid))
        job_id = row.last_job_id
        assert job_id is not None
        next_run = row.next_run_at.replace(tzinfo=dt.timezone.utc)  # SQLite drops tzinfo on round-trip
        assert next_run > utcnow()  # rescheduled to the following day
        assert row.schedule_claimed_at is None

    job = wait_job(client, auth, str(job_id))
    assert job["status"] == "succeeded" and job["rows_written"] == 2

    last = client.get(f"/api/pipelines/{pid}", headers=auth).json()["last_run"]
    assert last["job_id"] == str(job_id) and last["status"] == "succeeded"


def test_run_due_skips_a_claimed_or_not_yet_previewed_pipeline(client, auth, env):
    """A schedule alone can't fire a pipeline: it needs a real preview first, same guard /run enforces manually.
    A pipeline another tick already claimed is left alone too."""
    from app.db import get_sessionmaker
    from app.engine.schedule import run_due
    from app.models import Pipeline, utcnow

    src = _sqlite_conn(client, auth, _seed(env))
    dst = _sqlite_conn(client, auth, env / "dst.sqlite")
    r = client.post("/api/pipelines", json=_pipeline_body(src, dst, schedule="@daily"), headers=auth)
    pid = r.json()["id"]  # never previewed
    with get_sessionmaker()() as s:
        row = s.get(Pipeline, uuid.UUID(pid))
        row.next_run_at = utcnow() - dt.timedelta(minutes=1)
        s.commit()
    assert run_due() == 0
    assert client.get(f"/api/pipelines/{pid}", headers=auth).json()["last_run"] is None

    r2 = client.post("/api/pipelines", json=_pipeline_body(src, dst, schedule="@daily"), headers=auth)
    pid2 = r2.json()["id"]
    assert client.post(f"/api/pipelines/{pid2}/preview", json={"sample_rows": 50}, headers=auth).status_code == 200
    with get_sessionmaker()() as s:
        row = s.get(Pipeline, uuid.UUID(pid2))
        row.next_run_at = utcnow() - dt.timedelta(minutes=1)
        row.schedule_claimed_at = utcnow()  # another beat tick (or worker) is already running it
        s.commit()
    assert run_due() == 0


@needs_pg
def test_pipeline_end_to_end_against_real_postgres(client, auth):
    """The same run, against a real network-connected database rather than an embedded file."""
    pg = add_pg(client, auth)
    src_t, dst_t = uniq("pl_src"), uniq("pl_dst")
    pg_run(f"create table {src_t}(acct text, amt numeric, currency text)")
    pg_run(f"insert into {src_t} values ('a1', 100, 'USD'), ('a2', -5, 'EUR'), ('a3', 250, 'USD')")
    try:
        steps = [
            {"id": "s1", "kind": "rename", "config": {"mapping": {"acct": "account_id"}}},
            {"id": "s2", "kind": "filter", "config": {"expr": "amt > 0"}},
        ]
        body = _pipeline_body(pg, pg, steps=steps, source={"connection_id": pg, "table": f"public.{src_t}"},
                              target={"connection_id": pg, "table": f"public.{dst_t}", "mode": "append"})
        r = client.post("/api/pipelines", json=body, headers=auth)
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        pv = client.post(f"/api/pipelines/{pid}/preview", json={"sample_rows": 50}, headers=auth)
        assert pv.status_code == 200, pv.text
        assert [c["name"] for c in pv.json()["columns"]] == ["account_id", "amt", "currency"]

        r = client.post(f"/api/pipelines/{pid}/run", headers=auth)
        assert r.status_code == 200, r.text
        job = wait_job(client, auth, r.json()["id"])
        assert job["status"] == "succeeded" and job["rows_written"] == 2, job
        assert pg_run(f"select count(*) from {dst_t}")[0][0] == 2

        last = client.get(f"/api/pipelines/{pid}", headers=auth).json()["last_run"]
        assert last["status"] == "succeeded" and last["rows_written"] == 2
    finally:
        pg_run(f"drop table if exists {src_t}")
        pg_run(f"drop table if exists {dst_t}")
