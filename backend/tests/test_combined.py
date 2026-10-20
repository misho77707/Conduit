"""Combined sources (pipelines only): several inputs joined by one DuckDB SELECT. File databases stand in for the
different servers: SQLite (events), DuckDB (users), a CSV file (regions); no external services."""
import sqlite3


import duckdb
import pytest

from jobhelpers import add_ch, add_conn, add_pg, needs_ch, needs_pg, pg_run, uniq, wait_job


@pytest.fixture()
def world(client, auth, env):
    ev = env / "events.sqlite"
    c = sqlite3.connect(ev)
    c.execute("create table events(id integer primary key, user_id integer, amount double, ts timestamp)")
    c.executemany("insert into events values (?,?,?,?)",
                  [(i, i % 5 + 1, i * 1.5, f"2026-10-0{1 + i % 3} 10:{i % 60:02d}:00") for i in range(1, 31)])
    c.commit()
    c.close()
    us = env / "users.duckdb"
    d = duckdb.connect(str(us))
    d.execute("create table users(id integer, name varchar, country varchar)")
    d.execute("insert into users values (1,'ann','GE'),(2,'bob','AM'),(3,'cy','GE'),(4,'di','TR')")  # user 5 missing
    d.close()
    (env / "regions.csv").write_text("country,region\nGE,caucasus\nAM,caucasus\nTR,anatolia\n")
    sq = add_conn(client, auth, "sqlite", uniq("ev"), {"path": str(ev)})
    dk = add_conn(client, auth, "duckdb", uniq("us"), {"path": str(us), "read_only": True})
    dst = add_conn(client, auth, "sqlite", uniq("dst"), {"path": str(env / "dst.sqlite"), "create_if_missing": True})
    return {"sqlite": sq, "duckdb": dk, "dst": dst, "env": env}


def combined(w, sql, mode="staged", **kw):
    inputs = kw.pop("inputs", None) or [
        {"alias": "events", "source": {"connection_id": w["sqlite"], "table": "events"}},
        {"alias": "users", "source": {"connection_id": w["duckdb"], "table": "main.users"}},
    ]
    return {"combined": {"inputs": inputs, "sql": sql, "mode": mode, **kw}}


def pipeline(client, auth, w, source, table="out", **kw):
    r = client.post("/api/pipelines", json={"name": uniq("p"), "source": source,
                                            "target": {"connection_id": w["dst"], "table": table, **kw.pop("target", {})}, **kw},
                    headers=auth)
    return r


JOIN = "SELECT e.id, e.amount, u.name, u.country FROM events e LEFT JOIN users u ON u.id = e.user_id"


# ---- validation (offline) -----------------------------------------------------------------------------

@pytest.mark.parametrize("source, needle", [
    ({"combined": {"inputs": [{"alias": "a", "source": {"file": {"path": "/x.csv"}}}, {"alias": "A", "source": {"file": {"path": "/y.csv"}}}],
                   "sql": "SELECT * FROM a"}}, "used twice"),
    ({"combined": {"inputs": [{"alias": "a", "source": {"file": {"path": "/x.csv"}}}, {"alias": "b", "source": {"file": {"path": "/y.csv"}}}],
                   "sql": "SELECT * FROM a JOIN c ON true"}}, "reads c, which is not an input"),
    ({"combined": {"inputs": [{"alias": "a", "source": {"file": {"path": "/x.csv"}}}, {"alias": "b", "source": {"file": {"path": "/y.csv"}}}],
                   "sql": "SELECT * FROM a", "mode": "stream"}}, "Stream mode needs a driver"),
    ({"combined": {"inputs": [{"alias": "a", "source": {"file": {"path": "/x.csv"}}}, {"alias": "b", "source": {"file": {"path": "/y.csv"}}}],
                   "sql": "SELECT a.k, count(*) FROM a JOIN b ON a.k = b.k GROUP BY a.k", "mode": "stream", "driver": "a"}}, "GROUP BY"),
    ({"combined": {"inputs": [{"alias": "a", "source": {"file": {"path": "/x.csv"}}}, {"alias": "b", "source": {"file": {"path": "/y.csv"}}}],
                   "sql": "SELECT DISTINCT a.k FROM a", "mode": "stream", "driver": "a"}}, "DISTINCT"),
    ({"combined": {"inputs": [{"alias": "a", "source": {"file": {"path": "/x.csv"}}}, {"alias": "b", "source": {"file": {"path": "/y.csv"}}}],
                   "sql": "SELECT sum(a.v) FROM a", "mode": "stream", "driver": "a"}}, "aggregate"),
    ({"combined": {"inputs": [{"alias": "a", "source": {"file": {"path": "/x.csv"}}}, {"alias": "b", "source": {"file": {"path": "/y.csv"}}}],
                   "sql": "SELECT 1; SELECT 2"}}, "exactly one"),
    ({"combined": {"inputs": [{"alias": "a", "source": {"file": {"path": "/x.csv"}}}, {"alias": "b", "source": {"file": {"path": "/y.csv"}}}],
                   "sql": "DELETE FROM a"}}, "one SELECT"),
    ({"combined": {"inputs": [{"alias": "a", "source": {"file": {"path": "/x.csv"}}}, {"alias": "b", "source": {"file": {"path": "/y.csv"}}}],
                   "sql": "SELECT * FROM a", "watermark": {"column": "ts"}}}, "watermark needs a driver"),
    ({"combined": {"inputs": [{"alias": "1a", "source": {"file": {"path": "/x.csv"}}}, {"alias": "b", "source": {"file": {"path": "/y.csv"}}}],
                   "sql": "SELECT 1"}}, "plain identifier"),
    ({"combined": {"inputs": [{"alias": "a", "source": {"combined": {"inputs": [], "sql": "x"}}}, {"alias": "b", "source": {"file": {"path": "/y.csv"}}}],
                   "sql": "SELECT 1"}}, "Invalid pipeline"),
])
def test_validation(client, auth, world, source, needle):
    r = pipeline(client, auth, world, source)
    assert r.status_code == 422 and needle in r.text, r.text


def test_storage_connection_as_input_is_refused(client, auth, world):
    from moto import mock_aws

    with mock_aws():
        s3 = add_conn(client, auth, "s3", uniq("s3"), {"bucket": "b", "region": "us-east-1"}, {"access_key_id": "a", "secret_access_key": "b"})
        src = combined(world, "SELECT * FROM events", inputs=[
            {"alias": "events", "source": {"connection_id": s3, "table": "events"}},
            {"alias": "users", "source": {"connection_id": world["duckdb"], "table": "main.users"}}])
        r = pipeline(client, auth, world, src)
        assert r.status_code == 422 and "input events" in r.text and "file storage" in r.text, r.text


def test_run_needs_a_preview_first(client, auth, world):
    pid = pipeline(client, auth, world, combined(world, JOIN)).json()["id"]
    r = client.post(f"/api/pipelines/{pid}/run", headers=auth)
    assert r.status_code == 422 and "preview" in r.text.lower()


# ---- preview ----------------------------------------------------------------------------------------------

def test_preview_counts_schema_and_notes(client, auth, world):
    src = combined(world, JOIN + " ", inputs=[
        {"alias": "events", "source": {"connection_id": world["sqlite"], "table": "events"}, "columns": ["id", "user_id", "amount"]},
        {"alias": "users", "source": {"connection_id": world["duckdb"], "table": "main.users"}},
        {"alias": "regions", "source": {"file": {"path": str(world["env"] / "regions.csv")}}}])
    pid = pipeline(client, auth, world, src).json()["id"]
    pv = client.post(f"/api/pipelines/{pid}/preview", json={"sample_rows": 10}, headers=auth).json()
    assert [c["name"] for c in pv["columns"]] == ["id", "amount", "name", "country"], pv
    info = pv["combined"]
    assert [(i["alias"], i["rows"], i["capped"]) for i in info["inputs"]] == [("events", 30, False), ("users", 4, False), ("regions", 3, False)]
    assert [c["name"] for c in info["inputs"][0]["columns"]] == ["id", "user_id", "amount"]  # the column subset
    assert info["join_rows"] == 30 and pv["sampled_rows"] == 10
    assert any("regions is not used" in n for n in info["notes"])
    p = client.get(f"/api/pipelines/{pid}", headers=auth).json()
    assert [c["name"] for c in p["source_columns"]] == ["id", "amount", "name", "country"]  # cached for runs


def test_preview_schema_is_exact_with_no_matches_and_capped_note(client, auth, world, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("CONDUIT_COMBINED_PREVIEW_ROWS", "5")
    get_settings.cache_clear()
    sql = "SELECT e.id, u.name, e.amount * 2 AS double_amount FROM events e JOIN users u ON u.id = e.user_id + 1000"
    pid = pipeline(client, auth, world, combined(world, sql)).json()["id"]
    pv = client.post(f"/api/pipelines/{pid}/preview", json={"sample_rows": 10}, headers=auth).json()
    assert pv["sampled_rows"] == 0 and pv["combined"]["join_rows"] == 0
    assert [(c["name"], c["type"]) for c in pv["source_columns"]] == [("id", "BIGINT"), ("name", "VARCHAR"), ("double_amount", "DOUBLE")]
    notes = pv["combined"]["notes"]
    assert any("Preview joined the first 5 rows of events: matches may be missing" in n for n in notes), notes
    assert [(i["alias"], i["capped"]) for i in pv["combined"]["inputs"]] == [("events", True), ("users", False)]


def test_preview_reports_join_key_type_mismatch_and_steps_after_the_join(client, auth, world):
    sql = "SELECT e.id, u.name FROM events e LEFT JOIN users u ON u.name = e.user_id"
    steps = [{"id": "s1", "kind": "derive", "config": {"column": "tag", "expr": "'x'"}}]
    pid = pipeline(client, auth, world, combined(world, sql), steps=steps).json()["id"]
    pv = client.post(f"/api/pipelines/{pid}/preview", json={"sample_rows": 5}, headers=auth)
    # DuckDB refuses to compare text with a number; the error says which join keys disagree
    assert pv.status_code == 422 and "Join key types differ: users.name is VARCHAR, events.user_id is BIGINT" in pv.text, pv.text
    # a compatible join: steps run after it, upto_step still works
    client.patch(f"/api/pipelines/{pid}", json={"source": combined(world, JOIN)}, headers=auth)
    pv = client.post(f"/api/pipelines/{pid}/preview", json={"sample_rows": 5}, headers=auth).json()
    assert [c["name"] for c in pv["columns"]][-1] == "tag" and [c["name"] for c in pv["source_columns"]][-1] == "country"
    pv = client.post(f"/api/pipelines/{pid}/preview", json={"sample_rows": 5, "upto_step": "s1"}, headers=auth).json()
    assert pv["full_chain"] is False and [c["name"] for c in pv["columns"]][-1] == "tag"


def test_editing_the_source_clears_the_cached_schemas(client, auth, world):
    pid = pipeline(client, auth, world, combined(world, JOIN)).json()["id"]
    client.post(f"/api/pipelines/{pid}/preview", json={"sample_rows": 5}, headers=auth)
    client.patch(f"/api/pipelines/{pid}", json={"source": combined(world, "SELECT * FROM events JOIN users ON users.id = events.user_id")}, headers=auth)
    p = client.get(f"/api/pipelines/{pid}", headers=auth).json()
    assert p["output_columns"] is None and p["source_columns"] is None


# ---- staged runs ---------------------------------------------------------------------------------------

def run_pipeline(client, auth, pid, timeout=60):
    r = client.post(f"/api/pipelines/{pid}/run", headers=auth)
    assert r.status_code in (200, 201), r.text
    return wait_job(client, auth, r.json()["id"], timeout)


def ready(client, auth, w, source, **kw):
    r = pipeline(client, auth, w, source, **kw)
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    pv = client.post(f"/api/pipelines/{pid}/preview", json={"sample_rows": 5}, headers=auth)
    assert pv.status_code == 200, pv.text
    return pid


def dst_rows(w, sql):
    c = sqlite3.connect(w["env"] / "dst.sqlite")
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def log_of(client, auth, job_id):
    return " | ".join(x["msg"] for x in client.get(f"/api/jobs/{job_id}", headers=auth).json()["log"])


def test_staged_left_join_and_group_by_into_sqlite(client, auth, world):
    pid = ready(client, auth, world, combined(world, JOIN), table="joined")
    j = run_pipeline(client, auth, pid)
    assert j["status"] == "succeeded" and j["rows_written"] == 30 and j["rows_total_exact"], j
    assert dst_rows(world, "select count(*), count(name) from joined") == [(30, 24)]  # user 5 has no row: LEFT JOIN keeps 6
    detail = client.get(f"/api/jobs/{j['id']}", headers=auth).json()
    assert detail["phase"] == "succeeded" and any("events 30 rows" in n for n in detail["plan"]["notes"]), detail["plan"]
    assert "staged events: 30 rows" in log_of(client, auth, j["id"])
    from app.engine.combined_run import job_dir
    assert not job_dir(j["id"]).exists()  # the scratch file is gone once the job succeeded
    agg = ("SELECT u.country, count(*) AS n, sum(e.amount) AS total FROM events e JOIN users u ON u.id = e.user_id "
           "GROUP BY u.country ORDER BY u.country")
    pid = ready(client, auth, world, combined(world, agg), table="by_country")
    j = run_pipeline(client, auth, pid)
    assert j["status"] == "succeeded" and j["rows_written"] == 3, j
    assert dst_rows(world, "select country, n from by_country order by country") == [("AM", 6), ("GE", 12), ("TR", 6)]


def _add_events(w, rows):
    c = sqlite3.connect(w["env"] / "events.sqlite")
    c.executemany("insert into events values (?,?,?,?)", rows)
    c.commit()
    c.close()


def test_watermark_reads_only_new_driver_rows_and_sees_lookup_changes(client, auth, world):
    src = combined(world, JOIN, driver="events", watermark={"column": "id"})
    pid = ready(client, auth, world, src, table="wm")
    j = run_pipeline(client, auth, pid)
    assert j["rows_written"] == 30 and "watermark: first run" in log_of(client, auth, j["id"]), j
    _add_events(world, [(31, 1, 1.0, "2026-10-04 10:00:00"), (32, 2, 2.0, "2026-10-04 11:00:00")])
    d = duckdb.connect(str(world["env"] / "users.duckdb"))
    d.execute("update users set name = 'ANN' where id = 1")
    d.close()
    j = run_pipeline(client, auth, pid)
    assert j["status"] == "succeeded" and j["rows_written"] == 2, j
    assert "rows with id > 30" in log_of(client, auth, j["id"])
    assert dst_rows(world, "select id, name from wm where id > 30 order by id") == [(31, "ANN"), (32, "bob")]

    from app.db import get_sessionmaker
    from app.models import Watermark

    def saved():
        with get_sessionmaker()() as s:
            return [w.source_key for w in s.query(Watermark)]

    j = run_pipeline(client, auth, pid)  # nothing new: 0 rows, the watermark stays
    assert j["status"] == "succeeded" and j["rows_written"] == 0, j
    assert saved() == [f"pipeline:{pid}:events"]
    assert client.delete(f"/api/pipelines/{pid}", headers=auth).status_code == 204
    assert saved() == []  # deleting the pipeline deletes its watermark


def test_overlap_with_upsert(client, auth, world):
    sql = "SELECT e.id, e.ts, u.name FROM events e LEFT JOIN users u ON u.id = e.user_id"
    src = combined(world, sql, driver="events", watermark={"column": "ts", "overlap": 3600})
    pid = ready(client, auth, world, src, table="ov", target={"mode": "upsert", "keys": ["id"]})
    assert run_pipeline(client, auth, pid)["rows_written"] == 30
    # the newest ts loaded is 2026-10-03 10:29; a row arrives late with 10:10 - below the watermark, within the hour
    _add_events(world, [(40, 1, 1.0, "2026-10-03 10:10:00")])
    j = run_pipeline(client, auth, pid)
    # re-read: everything after 09:29 on the 3rd = the 10 rows of that day + the late one
    assert j["status"] == "succeeded" and j["rows_written"] == 11, j
    assert dst_rows(world, "select count(*), count(distinct id) from ov") == [(31, 31)]  # upsert: no duplicates
    assert dst_rows(world, "select count(*) from ov where id = 40") == [(1,)]


def test_retry_after_a_load_failure_reuses_the_scratch_file(client, auth, world, monkeypatch):
    from app.engine import combined_run

    staged = []
    real_stage, real_read = combined_run._stage_and_join, combined_run.StagedResult.read_batches
    monkeypatch.setattr(combined_run, "_stage_and_join", lambda *a, **k: staged.append(1) or real_stage(*a, **k))
    broken = [True]

    def flaky(self, req, split, batch_rows):
        if broken[0]:
            broken[0] = False
            raise RuntimeError("target went away")
        yield from real_read(self, req, split, batch_rows)

    monkeypatch.setattr(combined_run.StagedResult, "read_batches", flaky)
    pid = ready(client, auth, world, combined(world, JOIN), table="rt", options={"retries": 0})
    j = run_pipeline(client, auth, pid)
    assert j["status"] == "failed" and "target went away" in j["error"], j
    assert combined_run.job_dir(j["id"]).joinpath("state.json").exists()
    assert client.post(f"/api/jobs/{j['id']}/retry", headers=auth).status_code == 200
    j = wait_job(client, auth, j["id"])
    assert j["status"] == "succeeded" and j["rows_written"] == 30, j
    assert staged == [1]  # staged once: the retry only loaded
    assert "reusing the staged inputs" in log_of(client, auth, j["id"])


def test_disk_floor(client, auth, world, monkeypatch):
    from app.engine import combined_run

    pid = ready(client, auth, world, combined(world, JOIN), table="disk")
    monkeypatch.setattr(combined_run, "_floor_bytes", lambda p: (1, 5 * 1024 ** 3, 10 * 1024 ** 3))
    j = run_pipeline(client, auth, pid)
    assert j["status"] == "failed" and "Stopped before staging" in j["error"] and "floor" in j["error"], j
    calls = [0]

    def drops(p):  # enough space to start, then the disk fills up while staging
        calls[0] += 1
        return (100 * 1024 ** 3 if calls[0] == 1 else 1, 5 * 1024 ** 3, 200 * 1024 ** 3)

    monkeypatch.setattr(combined_run, "_floor_bytes", drops)
    monkeypatch.setattr("app.engine.combined_run.time.monotonic", lambda: calls[0] * 10.0)
    j = run_pipeline(client, auth, pid)
    assert j["status"] == "failed" and "Stopped staging" in j["error"], j


# ---- stream mode ----------------------------------------------------------------------------------------

def test_stream_mode_with_a_lookup(client, auth, world):
    src = combined(world, JOIN, mode="stream", driver="events")
    pid = ready(client, auth, world, src, table="streamed", options={"read_chunk": 100})
    j = run_pipeline(client, auth, pid)
    assert j["status"] == "succeeded" and j["rows_written"] == 30, j
    assert dst_rows(world, "select count(*), count(name) from streamed") == [(30, 24)]
    log = log_of(client, auth, j["id"])
    assert "lookup users: 4 rows in memory" in log and "staged events" not in log  # the driver is never copied
    detail = client.get(f"/api/jobs/{j['id']}", headers=auth).json()
    assert any("A Retry of a stream-mode run restarts the driver" in n for n in detail["plan"]["notes"])


def test_stream_mode_watermark(client, auth, world):
    src = combined(world, JOIN, mode="stream", driver="events", watermark={"column": "id"})
    pid = ready(client, auth, world, src, table="sw")
    assert run_pipeline(client, auth, pid)["rows_written"] == 30
    _add_events(world, [(50, 3, 5.0, "2026-10-05 10:00:00")])
    j = run_pipeline(client, auth, pid)
    assert j["status"] == "succeeded" and j["rows_written"] == 1, j
    assert dst_rows(world, "select id, name from sw where id = 50") == [(50, "cy")]


def test_a_lookup_over_the_cap_fails_early(client, auth, world, monkeypatch):
    from app.config import get_settings

    pid = ready(client, auth, world, combined(world, JOIN, mode="stream", driver="events"), table="cap")
    monkeypatch.setenv("CONDUIT_COMBINED_LOOKUP_MAX_ROWS", "3")  # users has 4 rows
    get_settings.cache_clear()
    j = run_pipeline(client, auth, pid)
    assert j["status"] == "failed" and "Lookup users has more than 3 rows" in j["error"] and "staged mode" in j["error"], j
    assert j["rows_written"] == 0


def test_mongodb_input_with_a_csv_lookup(client, auth, world, monkeypatch):
    mongomock = pytest.importorskip("mongomock")
    shared = mongomock.MongoClient()
    shared.close = lambda: None
    import app.connectors.mongo as cm

    monkeypatch.setattr(cm, "MongoClient", lambda *a, **k: shared)
    shared.shop.orders.insert_many([{"order": i, "country": c, "total": i * 10} for i, c in enumerate(["GE", "AM", "TR", "GE"], 1)])
    mg = add_conn(client, auth, "mongodb", uniq("mg"), {"database": "shop"}, {"uri": "mongodb://localhost:27017/shop"})
    src = combined(world, "SELECT o.order, o.total, r.region FROM orders o JOIN regions r ON r.country = o.country", inputs=[
        {"alias": "orders", "source": {"connection_id": mg, "table": "orders"}, "columns": ["order", "country", "total"]},
        {"alias": "regions", "source": {"file": {"path": str(world["env"] / "regions.csv")}}}])
    pid = ready(client, auth, world, src, table="by_region")
    j = run_pipeline(client, auth, pid)
    assert j["status"] == "succeeded" and j["rows_written"] == 4, j
    assert dst_rows(world, "select region, count(*) from by_region group by region order by region") == [("anatolia", 1), ("caucasus", 3)]


@needs_pg
@needs_ch
def test_live_clickhouse_events_with_a_postgres_dimension(client, auth):
    """ClickHouse events + a Postgres dimension into a Postgres target, both modes, then a daily GROUP BY."""
    from app.connectors import get_connector
    from jobhelpers import CH

    ev, dim, out = uniq("ev"), uniq("dim"), uniq("out")
    chc = get_connector("clickhouse", CH, {})._client()
    try:
        chc.command(f"CREATE TABLE conduit.{ev} (id UInt64, user_id UInt32, amount Decimal(12,2), at DateTime64(3)) "
                    "ENGINE = MergeTree ORDER BY id")
        chc.command(f"INSERT INTO conduit.{ev} SELECT number + 1, number % 50 + 1, number / 4, "
                    "toDateTime64('2026-10-01 00:00:00', 3) + number * 60 FROM numbers(5000)")
        pg_run(f"create table {dim}(id integer primary key, name text, country text)")
        pg_run(f"insert into {dim} select g, 'u' || g, case when g % 2 = 0 then 'GE' else 'AM' end from generate_series(1, 40) g")
        pg, ch = add_pg(client, auth), add_ch(client, auth)
        inputs = [{"alias": "events", "source": {"connection_id": ch, "table": f"conduit.{ev}"}},
                  {"alias": "users", "source": {"connection_id": pg, "table": f"public.{dim}"}}]
        sql = "SELECT e.id, e.amount, e.at, u.name, u.country FROM events e LEFT JOIN users u ON u.id = e.user_id"
        for mode, table in (("staged", f"public.{out}_s"), ("stream", f"public.{out}_t")):
            r = client.post("/api/pipelines", json={"name": uniq("p"), "target": {"connection_id": pg, "table": table},
                                                    "source": {"combined": {"inputs": inputs, "sql": sql, "mode": mode, "driver": "events"}}},
                            headers=auth)
            pid = r.json()["id"]
            assert client.post(f"/api/pipelines/{pid}/preview", json={"sample_rows": 5}, headers=auth).status_code == 200
            j = run_pipeline(client, auth, pid, timeout=180)
            assert j["status"] == "succeeded" and j["rows_written"] == 5000, (mode, j)
            assert pg_run(f"select count(*), count(name) from {table}") == [(5000, 4000)]  # users 41-50 are missing
        daily = ("SELECT CAST(e.at AS DATE) AS day, u.country, count(*) AS n, sum(e.amount) AS total FROM events e "
                 "JOIN users u ON u.id = e.user_id GROUP BY ALL")
        r = client.post("/api/pipelines", json={"name": uniq("p"), "target": {"connection_id": pg, "table": f"public.{out}_d"},
                                                "source": {"combined": {"inputs": inputs, "sql": daily}}}, headers=auth)
        pid = r.json()["id"]
        assert client.post(f"/api/pipelines/{pid}/preview", json={"sample_rows": 5}, headers=auth).status_code == 200
        j = run_pipeline(client, auth, pid, timeout=180)
        assert j["status"] == "succeeded", j
        assert pg_run(f"select sum(n) from public.{out}_d") == [(4000,)]
    finally:
        chc.command(f"DROP TABLE IF EXISTS conduit.{ev}")
        chc.close()


def test_the_scheduler_runs_a_combined_pipeline(client, auth, world):
    import uuid as _uuid
    from datetime import timedelta

    from app.db import get_sessionmaker
    from app.engine.schedule import run_due
    from app.models import Pipeline, utcnow

    pid = ready(client, auth, world, combined(world, JOIN), table="sched")
    client.patch(f"/api/pipelines/{pid}", json={"schedule": "* * * * *", "enabled": True}, headers=auth)
    with get_sessionmaker()() as s:
        s.get(Pipeline, _uuid.UUID(pid)).next_run_at = utcnow() - timedelta(minutes=1)
        s.commit()
    assert run_due() == 1
    p = client.get(f"/api/pipelines/{pid}", headers=auth).json()
    j = wait_job(client, auth, p["last_run"]["job_id"])
    assert j["status"] == "succeeded" and j["rows_written"] == 30, j
