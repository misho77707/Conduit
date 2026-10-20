"""Combined sources (pipelines only): several inputs joined by one DuckDB SELECT. File databases stand in for the
different servers: SQLite (events), DuckDB (users), a CSV file (regions); no external services."""
import sqlite3
import time

import duckdb
import pytest

from jobhelpers import add_conn, uniq, wait_job


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


def _unused(*a):  # keep imports used by later phases
    return time, wait_job
