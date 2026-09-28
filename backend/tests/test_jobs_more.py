"""Load engine against every database type and file format. Live servers are used where available;
MongoDB is exercised through mongomock (an in-memory stand-in, NOT a real server)."""
import csv
import datetime as dt
import sqlite3

import duckdb
import pytest

from jobhelpers import (add_ch, add_conn, add_my, add_pg, needs_ch, needs_mysql, needs_pg, pg_run, run, spec, uniq)

N = 3000


def _sqlite(env, name="a.sqlite", rows=N):
    p = env / name
    c = sqlite3.connect(p)
    c.execute("create table ev(id integer primary key, amount numeric, name varchar(20), ts timestamp, d date, ok boolean, raw blob)")
    c.executemany("insert into ev values (?,?,?,?,?,?,?)",
                  [(i, i / 4, f"n{i % 30}", (dt.datetime(2024, 1, 1) + dt.timedelta(minutes=i)).isoformat(" "),
                    (dt.date(2024, 1, 1) + dt.timedelta(days=i % 300)).isoformat(), i % 2, b"\x01\x02") for i in range(1, rows + 1)])
    c.commit()
    c.close()
    return p


def _conn(client, auth, type_, path, **extra):
    return add_conn(client, auth, type_, uniq(type_), {"path": str(path), "create_if_missing": True, **extra})


# ---- embedded databases (one writer at a time) ---------------------------------------------------------------

def test_sqlite_to_duckdb_and_back_with_parallel_readers(client, auth, env):
    s = _conn(client, auth, "sqlite", _sqlite(env))
    d = _conn(client, auth, "duckdb", env / "w.duckdb")
    j = run(client, auth, spec(s, "ev", d, "main.ev", options={"split_column": "id", "workers": 4}))
    assert j["status"] == "succeeded" and j["rows_written"] == N and j["splits_total"] >= 4, j
    con = duckdb.connect(str(env / "w.duckdb"), read_only=True)
    assert con.execute("select count(*), sum(amount), min(ts), max(d) from main.ev").fetchone() == (N, sum(i / 4 for i in range(1, N + 1)),
                                                                                                   dt.datetime(2024, 1, 1, 0, 1), dt.date(2024, 10, 26))
    con.close()
    back = _conn(client, auth, "sqlite", env / "b.sqlite")
    j = run(client, auth, spec(d, "main.ev", back, "ev2", target={"mode": "upsert", "keys": ["id"]}, options={"split_column": "id", "workers": 3}))
    assert j["status"] == "succeeded" and j["rows_written"] == N, j
    c = sqlite3.connect(env / "b.sqlite")
    assert c.execute("select count(*), count(distinct id) from ev2").fetchone() == (N, N)
    assert c.execute("select typeof(ts), typeof(d), ok from ev2 where id = 3").fetchone()[:2] == ("text", "text")
    c.close()


def test_sqlite_time_range_on_text_timestamps(client, auth, env):
    s = _conn(client, auth, "sqlite", _sqlite(env))
    d = _conn(client, auth, "duckdb", env / "w.duckdb")
    j = run(client, auth, spec(s, "ev", d, "main.part", options={"strategy": "time", "split_column": "ts", "lo": "2024-01-01 00:10",
                                                                  "hi": "2024-01-01 01:10", "workers": 2}))
    assert j["rows_written"] == 60, j


# ---- files -----------------------------------------------------------------------------------------------------

def _messy_csv(env):
    p = env / "m.csv"
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "amount", "posted", "name"])
        for i in range(1, 501):
            w.writerow([i, f"{i}.50", "2024-01-02", f"n{i}"])
        w.writerow([501, "abc", "2024-01-03", "bad amount"])
        w.writerow([502, "1", "03/01/2024", "bad date"])
        w.writerow([503, "1", "2024-01-04"])  # short row: parser reject
        w.writerow([504, "", "2024-01-05", "empty amount is NULL"])
    return p


@needs_pg
def test_csv_load_with_type_overrides_and_rejects(client, auth, env):
    pg, t = add_pg(client, auth), uniq("csv")
    body = {"source": {"file": {"path": str(_messy_csv(env))}},
            "target": {"connection_id": pg, "table": f"public.{t}", "mode": "append"},
            "columns": [{"source": "id", "type": "BIGINT"}, {"source": "amount", "type": "DECIMAL(10,2)"},
                        {"source": "posted", "type": "DATE"}, {"source": "name", "type": "VARCHAR(40)"}],
            "options": {"max_rejects": 100}}
    try:
        j = run(client, auth, body)
        assert j["status"] == "succeeded", j
        assert j["rows_written"] == 501 and j["rows_rejected"] == 3 and j["kind"] == "file_load"
        assert pg_run(f"select count(*), sum(amount) from public.{t}")[0][1] > 0
        assert pg_run(f"select count(*) from public.{t} where amount is null")[0][0] == 1  # the blank became NULL
        text = client.get(f"/api/jobs/{j['id']}/rejects", headers=auth).text
        assert "cannot convert 'abc'" in text and "03/01/2024" in text and "MISSING COLUMNS" in text
    finally:
        pg_run(f"drop table if exists public.{t}")


@needs_pg
def test_day_first_dates_need_a_format(client, auth, env):
    pg, t = add_pg(client, auth), uniq("df")
    (env / "d.csv").write_text("id,posted\n1,31/01/2024\n2,15/02/2024\n")
    cols = [{"source": "id", "type": "BIGINT"}, {"source": "posted", "type": "DATE"}]
    src = {"file": {"path": str(env / "d.csv")}}
    try:
        j = run(client, auth, {"source": src, "target": {"connection_id": pg, "table": f"public.{t}"}, "columns": cols})
        assert j["rows_written"] == 0 and j["rows_rejected"] == 2  # rejected, never guessed
        cols[1]["format"] = "%d/%m/%Y"
        j = run(client, auth, {"source": src, "target": {"connection_id": pg, "table": f"public.{t}", "mode": "replace"}, "columns": cols})
        assert j["rows_written"] == 2
        assert [str(r[0]) for r in pg_run(f"select posted from public.{t} order by id")] == ["2024-01-31", "2024-02-15"]
    finally:
        pg_run(f"drop table if exists public.{t}")


def test_too_many_rejects_stops_the_job(client, auth, env):
    d = _conn(client, auth, "duckdb", env / "w.duckdb")
    body = {"source": {"file": {"path": str(_messy_csv(env))}}, "target": {"connection_id": d, "table": "main.x"},
            "columns": [{"source": "id", "type": "BIGINT"}, {"source": "amount", "type": "INTEGER"}], "options": {"max_rejects": 5, "write_batch": 100, "read_chunk": 100}}
    j = run(client, auth, body)
    assert j["status"] == "failed" and "rejected" in j["error"]


def _make_stalled(job_id):
    """Pretend the worker died: the job says 'running' but has been silent for 5 minutes."""
    import datetime as dt2
    import uuid

    from app.db import get_sessionmaker
    from app.models import Job

    with get_sessionmaker()() as s:
        j = s.get(Job, uuid.UUID(job_id))
        j.status, j.finished_at = "running", None
        j.heartbeat_at = dt2.datetime.now(dt2.timezone.utc) - dt2.timedelta(minutes=5)
        s.commit()


def test_stalled_job_can_be_retried_or_cancelled(client, auth, env):
    from jobhelpers import wait_job

    d = _conn(client, auth, "duckdb", env / "w.duckdb")
    (env / "s.csv").write_text("id,v\n1,a\n2,b\n3,c\n")
    j = run(client, auth, {"source": {"file": {"path": str(env / "s.csv")}}, "target": {"connection_id": d, "table": "main.s"}})
    assert j["status"] == "succeeded"
    _make_stalled(j["id"])
    assert client.get(f"/api/jobs/{j['id']}", headers=auth).json()["status"] == "stalled"
    r = client.post(f"/api/jobs/{j['id']}/retry", headers=auth)  # a healthy running job would answer 409 here
    assert r.status_code == 200, r.text
    assert wait_job(client, auth, j["id"], 30)["status"] == "succeeded"
    _make_stalled(j["id"])
    r = client.post(f"/api/jobs/{j['id']}/cancel", headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"


def test_running_job_with_fresh_heartbeat_cannot_be_retried(client, auth, env):
    import datetime as dt2
    import uuid

    from app.db import get_sessionmaker
    from app.models import Job

    d = _conn(client, auth, "duckdb", env / "w.duckdb")
    (env / "s.csv").write_text("id,v\n1,a\n")
    j = run(client, auth, {"source": {"file": {"path": str(env / "s.csv")}}, "target": {"connection_id": d, "table": "main.s"}})
    with get_sessionmaker()() as s:
        row = s.get(Job, uuid.UUID(j["id"]))
        row.status, row.heartbeat_at = "running", dt2.datetime.now(dt2.timezone.utc)
        s.commit()
    assert client.post(f"/api/jobs/{j['id']}/retry", headers=auth).status_code == 409


def test_json_parquet_and_excel_sources(client, auth, env):
    import openpyxl

    d = _conn(client, auth, "duckdb", env / "w.duckdb")
    (env / "e.ndjson").write_text("\n".join(f'{{"id": {i}, "v": "x{i}"}}' for i in range(1, 101)))
    con = duckdb.connect()
    con.execute(f"copy (select range as id, 'p' || range as v from range(200)) to '{env / 'p.parquet'}' (format parquet)")
    con.close()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["id", "when", "v"])
    for i in range(1, 51):
        ws.append([i, dt.datetime(2024, 1, 1) + dt.timedelta(hours=i), f"s{i}"])
    wb.save(env / "s.xlsx")
    for fname, table, n in [("e.ndjson", "main.j", 100), ("p.parquet", "main.p", 200), ("s.xlsx", "main.x", 50)]:
        j = run(client, auth, {"source": {"file": {"path": str(env / fname)}}, "target": {"connection_id": d, "table": table}})
        assert j["status"] == "succeeded" and j["rows_written"] == n, (fname, j)
    con = duckdb.connect(str(env / "w.duckdb"), read_only=True)
    assert con.execute("select typeof(\"when\") from main.x limit 1").fetchone()[0] == "TIMESTAMP"
    con.close()


def test_upload_then_load(client, auth, env):
    d = _conn(client, auth, "duckdb", env / "w.duckdb")
    r = client.post("/api/files/upload", files={"file": ("../evil name.csv", b"a,b\n1,2\n3,4\n")}, headers=auth)
    assert r.status_code == 201, r.text
    up = r.json()
    assert up["name"] == "evil name.csv" and str(env) in up["path"]
    j = run(client, auth, {"source": {"file": {"path": up["path"]}}, "target": {"connection_id": d, "table": "main.up"}})
    assert j["rows_written"] == 2


def test_xls_is_refused_with_a_clear_message(client, auth, env):
    d = _conn(client, auth, "duckdb", env / "w.duckdb")
    (env / "old.xls").write_bytes(b"x")
    r = client.post("/api/jobs", json={"source": {"file": {"path": str(env / "old.xls")}}, "target": {"connection_id": d, "table": "main.o"}}, headers=auth)
    assert r.status_code == 422 and ".xlsx" in r.text


def test_duckdb_handles_survive_parallel_open_and_close(env):
    """Parallel splits open and close handles on one DuckDB file all the time. Plain duckdb.connect() fails this loop
    most runs with 'Unique file handle conflict' (verified while writing it)."""
    import random
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    import duckdb as ddb

    from app.connectors.duckdb_db import open_shared

    path = str(env / "w.duckdb")
    c0 = ddb.connect(path)
    c0.execute("create table t(a int)")
    c0.close()
    write_turn = threading.Semaphore(1)  # like the engine: one writer at a time, but opens and closes overlap

    def hammer(_):
        for _ in range(40):
            h = open_shared(path, False)
            try:
                with write_turn:
                    h.execute("insert into t select range from range(300)")
            finally:
                h.close()
            time.sleep(random.random() * 0.005)

    with ThreadPoolExecutor(6) as pool:
        for f in [pool.submit(hammer, i) for i in range(6)]:
            f.result()
    c = ddb.connect(path, read_only=True)
    assert c.execute("select count(*) from t").fetchone()[0] == 6 * 40 * 300
    c.close()


# ---- MySQL / MariaDB ---------------------------------------------------------------------------------------------

@needs_pg
@needs_mysql
def test_postgres_to_mysql_and_back(client, auth):
    pg, my, t = add_pg(client, auth), add_my(client, auth), uniq("mx")
    pg_run(f"create table {t}(id bigint primary key, amount numeric(12,2), name varchar(20), note text, ts timestamptz, d date, flag boolean, raw bytea)")
    pg_run(f"insert into {t} select g, g/4.0, 'n'||(g%30), case when g%7=0 then null else 'a%b '||g end, '2024-01-01'::timestamptz + (g||' minutes')::interval, "
           f"'2024-01-01'::date + (g%300), g%2=0, decode('00ff','hex') from generate_series(1,{N}) g")
    try:
        j = run(client, auth, spec(pg, f"public.{t}", my, f"conduit.{t}", target={"mode": "upsert", "keys": ["id"]}, options={"split_column": "id"}))
        assert j["status"] == "succeeded" and j["rows_written"] == N, j
        assert run(client, auth, spec(pg, f"public.{t}", my, f"conduit.{t}", target={"mode": "upsert", "keys": ["id"]}, options={"split_column": "id"}))["status"] == "succeeded"
        ins = client.get(f"/api/connections/{my}/tables/conduit.{t}/inspect", headers=auth).json()
        assert ins["candidates"][0]["column"] == "id" and ins["candidates"][0]["verdict"] == "recommended"
        j = run(client, auth, spec(my, f"conduit.{t}", pg, f"public.{t}_back", options={"strategy": "time", "split_column": "ts", "lo": "2024-01-01 00:00",
                                                                                         "hi": "2024-01-01 02:00"}))
        assert j["rows_written"] == 119, j  # minutes 1..119
        assert bytes(pg_run(f"select raw from public.{t}_back limit 1")[0][0]) == b"\x00\xff"
        # a filter containing a literal % must survive the driver's placeholder handling
        j = run(client, auth, spec(my, f"conduit.{t}", pg, f"public.{t}_pct", source={"where": "note like 'a%b %'"}))
        assert j["rows_written"] == N - N // 7
    finally:
        for x in (t, t + "_back", t + "_pct"):
            pg_run(f"drop table if exists public.{x}")
        from app.connectors import get_connector
        from jobhelpers import MY
        c = get_connector("mysql", MY, {"password": "conduit"})._connect()
        c.cursor().execute(f"drop table if exists `{t}`")
        c.close()


# ---- ClickHouse ----------------------------------------------------------------------------------------------------

@needs_pg
@needs_ch
def test_postgres_to_clickhouse_partitions_and_upsert(client, auth):
    pg, ch, t = add_pg(client, auth), add_ch(client, auth), uniq("cx")
    pg_run(f"create table {t}(id bigint primary key, amount numeric(12,2), name varchar(20), ts timestamptz, d date, ok boolean)")
    pg_run(f"insert into {t} select g, g/4.0, 'n'||(g%30), '2024-01-01'::timestamptz + (g*20||' minutes')::interval, '2024-01-01'::date + (g%300), g%2=0 "
           f"from generate_series(1,{N}) g")
    from app.connectors import get_connector
    from jobhelpers import CH
    chc = get_connector("clickhouse", CH, {})._client()
    try:
        j = run(client, auth, spec(pg, f"public.{t}", ch, f"conduit.{t}", target={"partition_by": "toYYYYMM(ts)", "order_by": ["ts", "id"]},
                                   options={"split_column": "id"}))
        assert j["status"] == "succeeded" and j["rows_written"] == N, j
        assert "PARTITION BY toYYYYMM(ts)" in "\n".join(j["plan"]["ddl"]) and "allow_nullable_key" in "\n".join(j["plan"]["ddl"])
        assert chc.query(f"select count(), sum(amount) from conduit.{t}").first_row[0] == N
        cand = {c["column"]: c for c in client.get(f"/api/connections/{ch}/tables/conduit.{t}/inspect", headers=auth).json()["candidates"]}
        assert cand["ts"]["verdict"] == "recommended" and cand["_partition_id"]["mode"] == "partition"  # the planner offers the partitions
        # read back split by partition
        j = run(client, auth, spec(ch, f"conduit.{t}", pg, f"public.{t}_back", options={"split_column": "_partition_id", "workers": 3}))
        assert j["status"] == "succeeded" and j["rows_written"] == N and j["splits_total"] > 1, j
        assert pg_run(f"select count(*), count(distinct id), sum(amount) from public.{t}_back")[0] == pg_run(f"select count(*), count(distinct id), sum(amount) from public.{t}")[0]
        # upsert twice: ReplacingMergeTree keeps one row per key when read with FINAL
        body = spec(pg, f"public.{t}", ch, f"conduit.{t}_up", target={"mode": "upsert", "keys": ["id"]}, options={"split_column": "id"})
        run(client, auth, body)
        assert run(client, auth, body)["status"] == "succeeded"
        assert chc.query(f"select count() from conduit.{t}_up final").first_row[0] == N
        # bad upsert target is explained
        r = client.post("/api/jobs", json=spec(pg, f"public.{t}", ch, f"conduit.{t}", target={"mode": "upsert", "keys": ["id"]}), headers=auth)
        assert r.status_code == 422 and "ReplacingMergeTree" in r.text
    finally:
        for x in (t, t + "_back"):
            pg_run(f"drop table if exists public.{x}")
        chc.command(f"drop table if exists conduit.{t}")
        chc.command(f"drop table if exists conduit.{t}_up")
        chc.close()


# ---- MongoDB (mongomock, an in-memory stand-in) ---------------------------------------------------------------------

@pytest.fixture()
def mongo(monkeypatch):
    mongomock = pytest.importorskip("mongomock")
    import mongomock.collection as mc

    orig = mc.BulkOperationBuilder.add_update
    monkeypatch.setattr(mc.BulkOperationBuilder, "add_update", lambda self, *a, **kw: orig(self, *a, **{k: v for k, v in kw.items() if k != "sort"}))
    shared = mongomock.MongoClient()
    shared.close = lambda: None
    import app.connectors.mongo as cm

    monkeypatch.setattr(cm, "MongoClient", lambda *a, **k: shared)
    return shared


def test_mongodb_source_and_target(client, auth, env, mongo):
    import bson

    mg = add_conn(client, auth, "mongodb", uniq("mg"), {"database": "d"}, {"uri": "mongodb://localhost/d"})
    mongo.d.ev.insert_many([{"_id": bson.ObjectId(), "n": i, "amt": bson.Decimal128(f"{i}.25"), "tags": ["a"], "meta": {"k": i},
                             "ts": dt.datetime(2024, 1, 1) + dt.timedelta(minutes=i)} for i in range(400)])
    d = _conn(client, auth, "duckdb", env / "w.duckdb")
    j = run(client, auth, spec(mg, "ev", d, "main.ev", options={"split_column": "_id", "workers": 3}))
    assert j["status"] == "succeeded" and j["rows_written"] == 400 and j["splits_total"] > 1, j
    con = duckdb.connect(str(env / "w.duckdb"), read_only=True)
    assert con.execute("select count(distinct _id), sum(n), any_value(tags) from main.ev").fetchone()[:2] == (400, sum(range(400)))
    con.close()
    j = run(client, auth, spec(mg, "ev", d, "main.ev_f", source={"where": '{"n": {"$lt": 10}}'}, options={"strategy": "id", "split_column": "n", "lo": 3, "hi": 8}))
    assert j["rows_written"] == 6, j
    # into MongoDB: upsert twice, no duplicates
    s = _conn(client, auth, "sqlite", _sqlite(env))
    body = spec(s, "ev", mg, "loaded", target={"mode": "upsert", "keys": ["id"]}, options={"split_column": "id"})
    assert run(client, auth, body)["rows_written"] == N and run(client, auth, body)["status"] == "succeeded"
    assert mongo.d.loaded.count_documents({}) == N
