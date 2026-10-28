"""ClickHouse pulls directly (engine/chpull.py): when it is offered, what it sends, and live runs against PostgreSQL /
MySQL -> ClickHouse through named collections the test creates the way a ClickHouse admin would."""
import datetime as dt
import secrets
import time
import uuid
from types import SimpleNamespace

import pytest

from jobhelpers import (
    CH_HOST, MY, MY_HOST, PG, add_ch, add_conn, needs_ch, needs_mysql, needs_pg, pg_run, run, spec, uniq, wait_job,
)


# ---- no services ----------------------------------------------------------------------

def test_pull_fields_on_connections_are_validated(client, auth):
    base = {"type": "postgres", "secrets": {"password": "x"}}
    ok = {"host": "db", "ch_collection": "pg_prod", "ch_address": "db.internal:5433"}
    r = client.post("/api/connections", json={**base, "name": uniq("pg"), "config": ok}, headers=auth)
    assert r.status_code == 201, r.text
    for config, msg in (({"host": "db", "ch_collection": "pg prod; DROP"}, "a plain name"),
                        ({"host": "db", "ch_address": "db:5432"}, "needs a ClickHouse named collection"),
                        ({"host": "db", "ch_collection": "c", "ch_address": "db:5432/x"}, "host or host:port")):
        r = client.post("/api/connections", json={**base, "name": uniq("pg"), "config": config}, headers=auth)
        assert r.status_code == 422 and msg in r.text, r.text
    types = {t["type"]: t for t in client.get("/api/meta/db-types", headers=auth).json()}
    assert types["postgres"]["ch_pull"] and types["mysql"]["ch_pull"] and not types["sqlite"]["ch_pull"]


def test_the_pull_needs_a_clickhouse_target(client, auth, env):
    import sqlite3

    db = env / "x.sqlite"
    with sqlite3.connect(db) as c:
        c.execute("create table t (id integer primary key, v text)")
        c.execute("insert into t values (1, 'a')")
    lite = add_conn(client, auth, "sqlite", uniq("lite"), {"path": str(db)})
    body = spec(lite, "t", lite, "t2")
    plan = client.post("/api/jobs/plan", json=body, headers=auth)
    assert plan.status_code == 200, plan.text
    assert plan.json()["transfer_method"] is None  # nothing to choose: the UI shows no transfer-method control
    body["options"]["transfer_method"] = "clickhouse_pull"
    r = client.post("/api/jobs/plan", json=body, headers=auth)
    assert r.status_code == 422 and "only from a PostgreSQL or MySQL table into a ClickHouse table" in r.json()["detail"]


def _fake(engine="MergeTree", collection="pg_prod", problem=None):
    tgt = SimpleNamespace(dialect="clickhouse", _db=lambda: "conduit", _q=lambda sql, params=None, **kw: [(engine,)])
    src = SimpleNamespace(ch_function="postgresql", ch_pull_problem=lambda req: problem)
    sconn = SimpleNamespace(config={"host": "db", "ch_collection": collection})
    return src, sconn, tgt


def _out(name="id", fmt=None):
    from app.engine.transform import NO_FILL, OutCol
    from app.engine.types import ColSpec

    return OutCol(name, ColSpec(name, "BIGINT", False), fmt, NO_FILL)


@pytest.mark.parametrize("change, why", [
    ({}, None),
    ({"steps": [{"id": "s1", "kind": "filter", "config": {}}], "step_output_columns": [{"name": "id", "type": "BIGINT"}]},
     "pipeline steps run in Conduit"),
    ({"source": {"query": "select 1 as id", "table": None}}, "custom SQL, and ClickHouse's postgresql() reads tables"),
    ({"source": {"where": "id > 3"}}, "WHERE filter is written in the source database's SQL"),
    ({"fmt": "%d.%m.%Y"}, "column `id` has a date format"),
    ({"split": "ctid"}, "ctid splits are PostgreSQL's own"),
    ({"collection": ""}, "no ClickHouse named collection"),
    ({"engine": "Log"}, "the target table is Log"),
    ({"engine": "ReplicatedMergeTree"}, "isn't replicated"),
    ({"problem": "column `tz` is timestamptz and ..."}, "column `tz` is timestamptz"),
])
def test_when_the_pull_is_offered(change, why):
    import uuid

    from app.engine.chpull import assess
    from app.engine.model import ReadRequest
    from app.engine.spec import JobIn

    cid = str(uuid.uuid4())
    body = {"source": {"connection_id": cid, "table": "public.t", **change.get("source", {})},
            "target": {"connection_id": cid, "table": "conduit.t"}}
    if "steps" in change:
        body |= {"steps": change["steps"], "step_output_columns": change["step_output_columns"]}
    s = JobIn.model_validate(body)
    src, sconn, tgt = _fake(change.get("engine", "MergeTree"), change.get("collection", "pg_prod"), change.get("problem"))
    req = ReadRequest(table=s.source.table, query=s.source.query, where=s.source.where, split_col=change.get("split"))
    m = assess(s, src, sconn, tgt, [_out(fmt=change.get("fmt"))], req, existing=True)
    assert m["eligible"] is (why is None) and m["used"] == "conduit"
    assert (m["why"] or "").find(why or "") >= 0 if why else m["why"] is None
    # not a ClickHouse target / a source ClickHouse can't read: there is no choice to show at all
    assert assess(s, src, sconn, SimpleNamespace(dialect="postgres"), [_out()], req, True) is None
    assert assess(s, SimpleNamespace(ch_function=None), sconn, tgt, [_out()], req, True) is None


def test_the_sql_names_the_collection_and_casts_to_the_target_types():
    from app.engine.chpull import Pull, _func, ch_message, split_conds
    from app.engine.model import ReadRequest, SplitSpec
    from app.engine.transform import OutCol
    from app.engine.types import ColSpec

    src = SimpleNamespace(ch_function="postgresql", ch_table_args=lambda t: {"schema": "public", "table": "o'rders"})
    for addr, tail in (("", ""), ("db.internal", ", host = 'db.internal'"), ("db.internal:6432", ", host = 'db.internal', port = 6432"),
                       ("[fd00::1]:5432", ", host = 'fd00::1', port = 5432")):
        f = _func(src, SimpleNamespace(config={"ch_collection": "pg_prod", "ch_address": addr}), "public.o'rders")
        assert f == "postgresql(pg_prod, schema = 'public', table = 'o\\'rders'" + tail + ")"
    oc = OutCol("amount", ColSpec("total", "DECIMAL(12,2)", False), None, 0)
    p = Pull("pg_prod", "postgresql(pg_prod)", [(oc, "`amount`")], {}, None)
    assert p.select_list({"total": "Decimal(12, 2)"}) == "CAST(ifNull(`amount`, 0) AS Decimal(12, 2)) AS `total`"
    assert p.narrow(1) == []
    p.key, p.first_key = "id", 7
    assert p.narrow(1) == ["`id` = 7"] and p.narrow(50) == ["`id` = 7"]
    p.last_key = 99
    assert p.narrow(50) == ["`id` >= 7", "`id` <= 99"]
    sp = SplitSpec(0, "x", "range", column="id", lo=10, hi=20)
    assert split_conds(ReadRequest(split_col="id"), sp) == ["`id` >= 10", "`id` < 20"]
    req = ReadRequest(split_col="ts", mode="time", lo=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc), exclusive_lo=True)
    assert split_conds(req, SplitSpec(0, "all", "full")) == ["`ts` > toDateTime64('2024-01-01 00:00:00.000000', 6, 'UTC')"]
    assert split_conds(ReadRequest(), SplitSpec(0, "all", "full")) == []
    e = Exception("Received ClickHouse exception, code: 60, server response: Code: 60. DB::Exception: MySQL table "
                  "`conduit`.`src` doesn't exist. (UNKNOWN_TABLE) (version 24.8.14.39 (official build)) (for url http://h:8123)")
    assert ch_message(e) == "MySQL table `conduit`.`src` doesn't exist. (UNKNOWN_TABLE)"


def test_utc_zones():
    from app.engine.adapters.base import is_utc

    assert all(is_utc(z) for z in ("UTC", "Etc/UTC", "+00:00", "GMT", "Universal"))
    assert not any(is_utc(z) for z in ("Europe/London", "Asia/Tbilisi", "+04:00", "SYSTEM", None))


# ---- live: PostgreSQL / MySQL -> ClickHouse ------------------------------------------------

def _ch():
    import clickhouse_connect

    return clickhouse_connect.get_client(host=CH_HOST, port=8123, username="default", database="conduit")


@pytest.fixture()
def pg_collection():
    """A PostgreSQL role with a password nothing else uses, and a named collection for it (the ClickHouse admin's job)."""
    role, pw, coll = uniq("pull"), "pw_" + secrets.token_hex(12), uniq("cc_pg")
    try:
        pg_run(f"create role {role} login password '{pw}'")
        pg_run(f"grant usage on schema public to {role}")
        pg_run(f"alter default privileges in schema public grant select on tables to {role}")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"needs a PostgreSQL user that can create roles: {e}")
    ch = _ch()
    try:
        ch.command(f"CREATE NAMED COLLECTION {coll} AS host = '{PG['host']}', port = {PG['port']}, user = '{role}', "
                   f"password = '{pw}', database = '{PG['database']}'")
    except Exception as e:  # noqa: BLE001
        pg_run(f"drop owned by {role}")
        pg_run(f"drop role {role}")
        pytest.skip(f"the ClickHouse user can't create named collections (named_collection_control): {e}")
    yield SimpleNamespace(name=coll, role=role, password=pw)
    ch.command(f"DROP NAMED COLLECTION IF EXISTS {coll}")
    pg_run(f"drop owned by {role}")
    pg_run(f"drop role {role}")


def _pg_source(client, auth, coll: str | None, **extra) -> str:
    return add_conn(client, auth, "postgres", uniq("pg"), {**PG, **({"ch_collection": coll} if coll else {}), **extra},
                    {"password": "conduit"})


def _typed_pg(t: str, rows: int = 2000) -> None:
    pg_run(f"create table {t} (id bigint primary key, i4 integer, d double precision, r real, n numeric(12,2), b boolean, tx text, "
           f"vc varchar(10), dt date, ts timestamp, tz timestamptz, by bytea, u uuid, jb jsonb)")
    pg_run(f"grant select on {t} to public")
    pg_run(f"insert into {t} select g, case when g % 7 = 0 then null else g * 3 end, g / 8.0 + 1 / (g + 2.0) * 1e-7, (g / 3.0)::real, g * 1.25, g % 2 = 0, "
           f"'héllo \"' || g || '\" ' || repeat('x', g % 5), 'v' || (g % 100), date '2024-02-29' + (g % 400), "
           f"timestamp '2024-01-01 12:00:00.123456' + make_interval(secs => g * 37.5), "
           f"timestamptz '2024-01-01 12:00:00+04' + make_interval(mins => g), decode(lpad(to_hex(g), 8, '0'), 'hex'), "
           f"md5(g::text)::uuid, jsonb_build_object('k', g) from generate_series(1, {rows}) g")


COMPARE = "id, i4, d, r, n, b, tx, vc, dt, ts, tz, hex(`by`), u"


def _ch_rows(table: str, cols: str = COMPARE):
    return _ch().query(f"SELECT {cols} FROM {table} ORDER BY id").result_rows


@needs_pg
@needs_ch
def test_postgres_pull_matches_the_conduit_path_and_logs_no_password(client, auth, pg_collection):
    src, a, b = uniq("pp"), uniq("via_conduit"), uniq("via_pull")
    _typed_pg(src)
    try:
        pg = _pg_source(client, auth, pg_collection.name)
        ch = add_ch(client, auth)
        opts = {"split_column": "id", "workers": 3, "splits": 4}
        plan = client.post("/api/jobs/plan", json=spec(pg, f"public.{src}", ch, f"conduit.{b}", options={**opts, "transfer_method": "clickhouse_pull"}), headers=auth)
        assert plan.status_code == 200, plan.text
        p = plan.json()
        assert p["transfer_method"]["used"] == "clickhouse_pull" and p["transfer_method"]["eligible"] and p["drivers"] == {}
        assert p["sample"]["rows"] > 0 and p["sample"]["preview"][0]["id"] == 1
        assert any(f"named collection `{pg_collection.name}`" in n for n in p["notes"])
        start = _ch().query("SELECT now()").result_rows[0][0]
        j1 = run(client, auth, spec(pg, f"public.{src}", ch, f"conduit.{a}", options=opts))
        j2 = run(client, auth, spec(pg, f"public.{src}", ch, f"conduit.{b}", options={**opts, "transfer_method": "clickhouse_pull"}))
        assert j1["status"] == j2["status"] == "succeeded", (j1, j2)
        assert j2["rows_written"] == 2000 and j2["splits_total"] == 4 and j2["plan"]["transfer_method"]["used"] == "clickhouse_pull"
        assert _ch_rows(f"conduit.{a}") == _ch_rows(f"conduit.{b}")  # identical values, both ways
        # JSON comes out as PostgreSQL prints jsonb (the Conduit path re-serialises it): same documents
        import json
        assert [json.loads(r[0]) for r in _ch_rows(f"conduit.{a}", "jb")] == [json.loads(r[0]) for r in _ch_rows(f"conduit.{b}", "jb")]
        assert _ch().query("SELECT count() FROM system.tables WHERE database = 'conduit' AND name LIKE {p:String}",
                           parameters={"p": f"conduit_pull_{j2['id'].replace('-', '')[:12]}%"}).result_rows[0][0] == 0

        ch_admin = _ch()
        ch_admin.command("SYSTEM FLUSH LOGS")
        pulls = ch_admin.query("SELECT count() FROM system.query_log WHERE query_id LIKE {q:String} AND type = 'QueryFinish'",
                               parameters={"q": f"conduit-{j2['id']}-%"}).result_rows[0][0]
        assert pulls == 4  # one INSERT … SELECT per split, and the test below is not vacuous
        read = ch_admin.query("SELECT sum(read_rows) FROM system.query_log WHERE query_id LIKE {q:String} AND type = 'QueryFinish'",
                              parameters={"q": f"conduit-{j2['id']}-%"}).result_rows[0][0]
        assert read == 2000  # each split's range reached PostgreSQL: no split read the whole table
        leaks = ch_admin.query("SELECT count() FROM system.query_log WHERE event_time >= {s:DateTime} AND position(query, {pw:String}) > 0 "
                               "AND query NOT ILIKE 'CREATE NAMED COLLECTION%'", parameters={"s": start, "pw": pg_collection.password}).result_rows[0][0]
        assert leaks == 0
        exc = ch_admin.query("SELECT count() FROM system.query_log WHERE event_time >= {s:DateTime} AND position(exception, {pw:String}) > 0",
                             parameters={"s": start, "pw": pg_collection.password}).result_rows[0][0]
        assert exc == 0
        # why Conduit never creates a collection itself: ClickHouse logs that statement with the password in it
        created = ch_admin.query("SELECT count() FROM system.query_log WHERE query ILIKE 'CREATE NAMED COLLECTION%' AND position(query, {pw:String}) > 0",
                                 parameters={"pw": pg_collection.password}).result_rows[0][0]
        assert created >= 1
    finally:
        pg_run(f"drop table if exists {src}")
        for t in (a, b):
            _ch().command(f"DROP TABLE IF EXISTS conduit.{t}")


@needs_pg
@needs_ch
def test_when_postgres_cannot_be_pulled_the_reason_is_shown(client, auth, pg_collection):
    src = uniq("pe")
    _typed_pg(src, 10)
    pg_run(f"create table {src}_arr (id int primary key, tags text[])")
    try:
        ch = add_ch(client, auth)
        bare = _pg_source(client, auth, None)
        p = client.post("/api/jobs/plan", json=spec(bare, f"public.{src}", ch, f"conduit.{src}"), headers=auth).json()
        assert p["transfer_method"]["eligible"] is False and "no ClickHouse named collection" in p["transfer_method"]["why"]
        assert p["transfer_method"]["limits"] and p["drivers"]  # the ordinary path still has its drivers
        pg = _pg_source(client, auth, pg_collection.name)
        p = client.post("/api/jobs/plan", json=spec(pg, None, ch, f"conduit.{src}", source={"table": None, "query": f"select * from {src}"}), headers=auth).json()
        assert "custom SQL" in p["transfer_method"]["why"]

        def refused(body, text):
            body["options"]["transfer_method"] = "clickhouse_pull"
            r = client.post("/api/jobs/plan", json=body, headers=auth)
            assert r.status_code == 422 and text in r.json()["detail"], r.text

        refused(spec(bare, f"public.{src}", ch, f"conduit.{src}"), "ClickHouse can't pull this directly: the source connection has no ClickHouse named collection")
        refused(spec(_pg_source(client, auth, "no_such_collection"), f"public.{src}", ch, f"conduit.{src}"),
                "ClickHouse can't read public." + src + " through the named collection `no_such_collection`")
        refused(spec(pg, f"public.{src}_arr", ch, f"conduit.{src}_arr"), "column `tags` is an array")
        _ch().command(f"CREATE TABLE conduit.{src}_log (id Int64) ENGINE = Log")
        refused(spec(pg, f"public.{src}", ch, f"conduit.{src}_log", columns=[{"source": "id"}]), "the target table is Log")
        # timestamptz on a server that isn't on UTC: ClickHouse would drop the offset
        pg_run(f"alter database {PG['database']} set timezone = 'Asia/Tbilisi'")
        try:
            refused(spec(pg, f"public.{src}", ch, f"conduit.{src}"), "column `tz` is timestamptz and the server's time zone is Asia/Tbilisi")
            body = spec(pg, f"public.{src}", ch, f"conduit.{src}", columns=[{"source": "id"}, {"source": "ts"}])
            body["options"]["transfer_method"] = "clickhouse_pull"
            assert client.post("/api/jobs/plan", json=body, headers=auth).status_code == 200  # no timestamptz column: fine
        finally:
            pg_run(f"alter database {PG['database']} reset timezone")
    finally:
        pg_run(f"drop table if exists {src}")
        pg_run(f"drop table if exists {src}_arr")
        _ch().command(f"DROP TABLE IF EXISTS conduit.{src}_log")


@needs_pg
@needs_ch
def test_a_failed_split_redoes_its_pull_and_a_failed_move_continues(client, auth, pg_collection, monkeypatch):
    from app.engine import chpull

    src, dst = uniq("pr"), uniq("pr_dst")
    _typed_pg(src, 3000)
    real_pull, real_save = chpull._pull, chpull._save
    hits = {"pull": 0, "move": 0}

    def flaky_pull(ctx, idx, sp, client_, stg, target):
        real_pull(ctx, idx, sp, client_, stg, target)  # rows staged ...
        if idx == 1 and not hits["pull"]:
            hits["pull"] += 1
            raise chpull.PullError("ClickHouse: connection reset by peer")  # ... then lost: staging is redone

    def flaky_save(job_id, idx, **values):
        real_save(job_id, idx, **values)
        moving = isinstance(values.get("resume"), dict) and str(values["resume"].get("v", "")).startswith("pull:moving:")
        if idx == 2 and moving and not hits["move"]:
            hits["move"] += 1
            raise chpull.PullError("ClickHouse: connection reset by peer")  # failed between staging and moving

    monkeypatch.setattr(chpull, "_pull", flaky_pull)
    monkeypatch.setattr(chpull, "_save", flaky_save)
    try:
        pg = _pg_source(client, auth, pg_collection.name)
        ch = add_ch(client, auth)
        j = run(client, auth, spec(pg, f"public.{src}", ch, f"conduit.{dst}",
                                   options={"split_column": "id", "splits": 3, "workers": 3, "transfer_method": "clickhouse_pull"}))
        assert j["status"] == "succeeded", j
        assert hits == {"pull": 1, "move": 1}
        assert [s["attempts"] for s in j["splits"]] == [1, 2, 2]
        assert _ch().query(f"SELECT count(), uniqExact(id) FROM conduit.{dst}").result_rows[0] == (3000, 3000)
        assert j["rows_written"] == 3000
    finally:
        pg_run(f"drop table if exists {src}")
        _ch().command(f"DROP TABLE IF EXISTS conduit.{dst}")


@needs_pg
@needs_ch
def test_splits_finishing_together_move_in_turn(client, auth, pg_collection, monkeypatch):
    """Splits that reach their move at the same moment: staging tables name their parts alike (all_1_1_0), and two
    concurrent MOVE PARTITIONs of same-named parts fail in ClickHouse ("Temporary part … already added"). A barrier
    makes all four arrive together; the per-target lock then lets them through one at a time."""
    import threading

    from app.engine import chpull

    src, dst = uniq("pc"), uniq("pc_dst")
    _typed_pg(src, 4000)
    real, barrier = chpull._move_lock, threading.Barrier(4, timeout=60)

    class Together:
        def __init__(self, lock):
            self.lock = lock

        def __enter__(self):
            barrier.wait()
            return self.lock.__enter__()

        def __exit__(self, *exc):
            return self.lock.__exit__(*exc)

    monkeypatch.setattr(chpull, "_move_lock", lambda target: Together(real(target)))
    try:
        pg = _pg_source(client, auth, pg_collection.name)
        ch = add_ch(client, auth)
        j = run(client, auth, spec(pg, f"public.{src}", ch, f"conduit.{dst}",
                                   options={"split_column": "id", "splits": 4, "workers": 4, "transfer_method": "clickhouse_pull"}))
        assert j["status"] == "succeeded", j
        assert _ch().query(f"SELECT count(), uniqExact(id) FROM conduit.{dst}").result_rows[0] == (4000, 4000)
    finally:
        pg_run(f"drop table if exists {src}")
        _ch().command(f"DROP TABLE IF EXISTS conduit.{dst}")


@needs_pg
@needs_ch
def test_a_failed_move_is_finished_by_retry_without_duplicates(client, auth, pg_collection, monkeypatch):
    from app.engine import chpull

    src, dst = uniq("pm"), uniq("pm_dst")
    _typed_pg(src, 1000)
    real_save = chpull._save
    state = {"fail": True}

    def failing_save(job_id, idx, **values):
        real_save(job_id, idx, **values)
        if state["fail"] and str((values.get("resume") or {}).get("v", "")).startswith("pull:moving:"):
            raise chpull.PullError("ClickHouse: Code: 241. Memory limit exceeded")  # not transient: the job fails

    monkeypatch.setattr(chpull, "_save", failing_save)
    try:
        pg = _pg_source(client, auth, pg_collection.name)
        ch = add_ch(client, auth)
        j = run(client, auth, spec(pg, f"public.{src}", ch, f"conduit.{dst}",
                                   options={"split_column": "id", "splits": 2, "workers": 1, "retries": 0, "transfer_method": "clickhouse_pull"}))
        assert j["status"] == "failed" and "Memory limit exceeded" in j["error"], j
        staged = _ch().query("SELECT name FROM system.tables WHERE database = 'conduit' AND name = {n:String}",
                             parameters={"n": chpull.staging_name(uuid.UUID(j["id"]), 0)}).result_rows
        assert staged  # kept for the retry: its rows were complete
        state["fail"] = False
        assert client.post(f"/api/jobs/{j['id']}/retry", headers=auth).status_code == 200
        j = wait_job(client, auth, j["id"])
        assert j["status"] == "succeeded", j
        assert _ch().query(f"SELECT count(), uniqExact(id) FROM conduit.{dst}").result_rows[0] == (1000, 1000)
        assert _ch().query("SELECT count() FROM system.tables WHERE database = 'conduit' AND name LIKE {p:String}",
                           parameters={"p": f"conduit_pull_{j['id'].replace('-', '')[:12]}%"}).result_rows[0][0] == 0
    finally:
        pg_run(f"drop table if exists {src}")
        _ch().command(f"DROP TABLE IF EXISTS conduit.{dst}")


@needs_pg
@needs_ch
def test_cancel_kills_the_clickhouse_query(client, auth, pg_collection):
    view, dst = uniq("slow"), uniq("slow_dst")
    # a source that takes ~10 s to read: ClickHouse streams it while PostgreSQL sleeps every 10,000 rows
    pg_run(f"create view {view} as select g::bigint as id, md5(g::text) as v from generate_series(1, 200000) g "
           f"cross join lateral pg_sleep(case when g % 10000 = 0 then 0.5 else 0 end) s")
    pg_run(f"grant select on {view} to public")
    try:
        pg = _pg_source(client, auth, pg_collection.name)
        ch = add_ch(client, auth)
        r = client.post("/api/jobs", json=spec(pg, f"public.{view}", ch, f"conduit.{dst}", options={"transfer_method": "clickhouse_pull"},
                                               target={"order_by": ["id"]}), headers=auth)
        assert r.status_code == 201, r.text
        jid = r.json()["id"]
        end = time.time() + 30
        while time.time() < end:
            if _ch().query("SELECT count() FROM system.processes WHERE query_id LIKE {q:String}", parameters={"q": f"conduit-{jid}-%"}).result_rows[0][0]:
                break
            time.sleep(0.2)
        else:
            raise AssertionError("the pull never started")
        end = time.time() + 8  # system.processes moves a read block (~65k rows, ~3 s of this source) at a time
        while time.time() < end:
            mid = client.get(f"/api/jobs/{jid}", headers=auth).json()
            if mid["rows_written"]:
                break
            time.sleep(0.25)
        assert mid["status"] == "running" and 0 < mid["rows_written"] < 200000, mid  # progress from system.processes
        t0 = time.time()
        assert client.post(f"/api/jobs/{jid}/cancel", headers=auth).status_code == 200
        j = wait_job(client, auth, jid, timeout=30)
        assert j["status"] == "cancelled" and time.time() - t0 < 8, j
        assert _ch().query("SELECT count() FROM system.processes WHERE query_id LIKE {q:String}", parameters={"q": f"conduit-{jid}-%"}).result_rows[0][0] == 0
        assert _ch().query("SELECT count() FROM system.tables WHERE database = 'conduit' AND name LIKE {p:String}",
                           parameters={"p": f"conduit_pull_{jid.replace('-', '')[:12]}%"}).result_rows[0][0] == 0
        assert _ch().query(f"SELECT count() FROM conduit.{dst}").result_rows[0][0] == 0  # nothing half-loaded
    finally:
        pg_run(f"drop view if exists {view}")
        _ch().command(f"DROP TABLE IF EXISTS conduit.{dst}")


@needs_pg
@needs_ch
def test_pull_modes_and_watermark(client, auth, pg_collection):
    src, dst = uniq("pw"), uniq("pw_dst")
    _typed_pg(src, 500)
    try:
        pg = _pg_source(client, auth, pg_collection.name)
        ch = add_ch(client, auth)
        wm = {"strategy": "watermark", "split_column": "id", "splits": 2, "transfer_method": "clickhouse_pull"}
        j = run(client, auth, spec(pg, f"public.{src}", ch, f"conduit.{dst}", options=wm))
        assert j["status"] == "succeeded" and j["rows_written"] == 500, j
        pg_run(f"insert into {src} (id, tx) select g, 'new' from generate_series(501, 540) g")
        j = run(client, auth, spec(pg, f"public.{src}", ch, f"conduit.{dst}", options=wm))
        assert j["status"] == "succeeded" and j["rows_written"] == 40, j  # only rows above the saved watermark
        assert _ch().query(f"SELECT count(), max(id) FROM conduit.{dst}").result_rows[0] == (540, 540)
        j = run(client, auth, spec(pg, f"public.{src}", ch, f"conduit.{dst}", target={"mode": "replace"},
                                   options={"split_column": "id", "transfer_method": "clickhouse_pull"}))
        assert j["status"] == "succeeded" and _ch().query(f"SELECT count() FROM conduit.{dst}").result_rows[0][0] == 540
        up = uniq("pw_up")
        body = spec(pg, f"public.{src}", ch, f"conduit.{up}", target={"mode": "upsert", "keys": ["id"]},
                    options={"split_column": "id", "transfer_method": "clickhouse_pull"})
        assert run(client, auth, body)["status"] == "succeeded"
        pg_run(f"update {src} set tx = 'changed' where id <= 10")
        assert run(client, auth, body)["status"] == "succeeded"
        rows = _ch().query(f"SELECT count(), countIf(tx = 'changed') FROM conduit.{up} FINAL").result_rows[0]
        assert rows == (540, 10)
        _ch().command(f"DROP TABLE IF EXISTS conduit.{up}")
    finally:
        pg_run(f"drop table if exists {src}")
        _ch().command(f"DROP TABLE IF EXISTS conduit.{dst}")


# ---- MySQL ---------------------------------------------------------------------------------

def my_run(sql, params=None, **kw):
    import pymysql

    c = pymysql.connect(host=MY["host"], port=MY["port"], user="conduit", password="conduit", database=MY["database"],
                        autocommit=True, **kw)
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else None
    finally:
        c.close()


@pytest.fixture()
def my_collection():
    coll = uniq("cc_my")
    try:
        _ch().command(f"CREATE NAMED COLLECTION {coll} AS host = '{MY_HOST}', port = 3306, user = 'conduit', "
                      f"password = 'conduit', database = '{MY['database']}'")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"the ClickHouse user can't create named collections (named_collection_control): {e}")
    yield coll
    _ch().command(f"DROP NAMED COLLECTION IF EXISTS {coll}")


@needs_mysql
@needs_ch
def test_mysql_pull_matches_the_conduit_path(client, auth, my_collection):
    src, a, b = uniq("mp"), uniq("my_conduit"), uniq("my_pull")
    my_run(f"create table {src} (id bigint primary key, n decimal(12,2), d double, b tinyint(1), vc varchar(20), tx text, "
           f"dt date, dtm datetime(6), ts timestamp(6) null, bl blob, j json)")
    my_run(f"insert into {src} values " + ", ".join(
        f"({i}, {i * 1.25}, {i / 8}, {i % 2}, 'v{i}', 'héllo \"{i}\"', '2024-02-29' + interval {i % 300} day, "
        f"'2024-01-01 12:00:00.123456' + interval {i * 37} second, '2024-01-01 12:00:00.5' + interval {i} minute, "
        f"x'{i:08x}', '{{\"k\": {i}}}')" for i in range(1, 801)), init_command="SET time_zone = '+00:00'")
    my_run(f"insert into {src} (id) values (900)")
    try:
        my = add_conn(client, auth, "mysql", uniq("my"), {**MY, "ch_collection": my_collection}, {"password": "conduit"})
        ch = add_ch(client, auth)
        opts = {"split_column": "id", "splits": 3, "workers": 3}
        pull = {**opts, "transfer_method": "clickhouse_pull"}
        r = client.post("/api/jobs/plan", json=spec(my, f"{MY['database']}.{src}", ch, f"conduit.{b}", options=pull), headers=auth)
        assert r.status_code == 422 and "column `d` is a floating-point column" in r.json()["detail"], r.text
        no_float = [{"source": c} for c in ("id", "n", "b", "vc", "tx", "dt", "dtm", "ts", "bl", "j")]
        p = client.post("/api/jobs/plan", json=spec(my, f"{MY['database']}.{src}", ch, f"conduit.{b}", options=pull, columns=no_float), headers=auth)
        assert p.status_code == 200, p.text
        assert 0 < p.json()["sample"]["rows"] <= 2000  # narrowed on the key: MySQL doesn't get the LIMIT
        j1 = run(client, auth, spec(my, f"{MY['database']}.{src}", ch, f"conduit.{a}", options=opts, columns=no_float))
        j2 = run(client, auth, spec(my, f"{MY['database']}.{src}", ch, f"conduit.{b}", options=pull, columns=no_float))
        assert j1["status"] == j2["status"] == "succeeded", (j1, j2)
        admin = _ch()
        admin.command("SYSTEM FLUSH LOGS")
        assert admin.query("SELECT sum(read_rows) FROM system.query_log WHERE query_id LIKE {q:String} AND type = 'QueryFinish'",
                           parameters={"q": f"conduit-{j2['id']}-%"}).result_rows[0][0] == 801  # ranges pushed down
        cols = "id, n, b, vc, tx, dt, dtm, ts, hex(bl)"
        assert _ch_rows(f"conduit.{a}", cols) == _ch_rows(f"conduit.{b}", cols)
        assert _ch_rows(f"conduit.{b}", "dtm, ts")[0] == (dt.datetime(2024, 1, 1, 12, 0, 37, 123456), dt.datetime(2024, 1, 1, 12, 1, 0, 500000))

        my_run("SET GLOBAL time_zone = '+04:00'")
        try:
            r = client.post("/api/jobs/plan", json=spec(my, f"{MY['database']}.{src}", ch, f"conduit.{b}", options=pull, columns=no_float), headers=auth)
            assert r.status_code == 422 and "column `ts` is TIMESTAMP and the server's time zone is +04:00" in r.json()["detail"], r.text
        finally:
            my_run("SET GLOBAL time_zone = 'SYSTEM'")
    finally:
        my_run(f"drop table if exists {src}")
        for t in (a, b):
            _ch().command(f"DROP TABLE IF EXISTS conduit.{t}")
