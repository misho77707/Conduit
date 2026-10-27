import pytest

from jobhelpers import add_pg, needs_pg, pg_run, run, spec, uniq, wait_job

pytestmark = needs_pg

N = 6000


@pytest.fixture()
def src_table():
    t = uniq("src")
    pg_run(f"create table {t}(id bigint primary key, amount numeric(12,2), name varchar(20), note text, ts timestamptz, d date, "
           f"flag boolean, raw bytea, j jsonb)")
    pg_run(f"insert into {t} select g, (g%1000)::numeric/7, 'n'||(g%50), case when g%10=0 then null else 'note '||g end, "
           f"'2024-01-01'::timestamptz + (g||' minutes')::interval, '2024-01-01'::date + (g%365), g%2=0, decode('deadbeef','hex'), "
           f"jsonb_build_object('a',g) from generate_series(1,{N}) g")
    pg_run(f"analyze {t}")
    made = [t]
    yield t
    for x in made + [t + "_dst"]:
        pg_run(f"drop table if exists {x}")


def _checksum(t: str):
    return pg_run(f"select count(*), count(distinct id), sum(amount), min(ts), max(ts), sum(length(note)), "
                  f"md5(string_agg(id::text||coalesce(note,'')||name||encode(raw,'hex')||j::text, ',' order by id)) from {t}")[0]


def test_full_copy_is_parallel_and_identical(client, auth, src_table):
    pg = add_pg(client, auth)
    dst = src_table + "_dst"
    j = run(client, auth, spec(pg, f"public.{src_table}", pg, f"public.{dst}", options={"split_column": "id", "workers": 4}))
    assert j["status"] == "succeeded", j
    assert j["rows_written"] == N and j["rows_rejected"] == 0 and j["progress"] == 1.0
    assert j["splits_total"] >= 4 and all(s["status"] == "done" for s in j["splits"])
    assert j["plan"]["split_column"] == "id" and any("CREATE TABLE" in d for d in j["plan"]["ddl"])
    assert _checksum(src_table) == _checksum(dst)
    assert any("finished" in line["msg"] for line in j["log"])


def test_id_range_is_inclusive_and_time_range_excludes_upper(client, auth, src_table):
    pg = add_pg(client, auth)
    d1, d2 = src_table + "_dst", src_table + "_dst2"
    j = run(client, auth, spec(pg, f"public.{src_table}", pg, f"public.{d1}",
                               options={"strategy": "id", "split_column": "id", "lo": 100, "hi": 199, "workers": 2}))
    assert j["rows_written"] == 100  # 100..199 inclusive
    assert pg_run(f"select min(id), max(id) from {d1}")[0] == (100, 199)
    j = run(client, auth, spec(pg, f"public.{src_table}", pg, f"public.{d2}",
                               options={"strategy": "time", "split_column": "ts", "lo": "2024-01-01 00:10", "hi": "2024-01-01 01:10",
                                        "timezone": "UTC"}))
    assert j["rows_written"] == 60  # minutes 10..69: the upper bound is exclusive
    pg_run(f"drop table {d1}")
    pg_run(f"drop table {d2}")


def test_time_bounds_use_the_given_timezone(client, auth, src_table):
    pg = add_pg(client, auth)
    dst = src_table + "_dst"
    # 04:00 in Tbilisi (UTC+4) is 00:00 UTC, i.e. the first minute of the data
    j = run(client, auth, spec(pg, f"public.{src_table}", pg, f"public.{dst}",
                               options={"strategy": "time", "split_column": "ts", "lo": "2024-01-01 04:00", "hi": "2024-01-01 05:00",
                                        "timezone": "Asia/Tbilisi"}))
    assert j["rows_written"] == 59  # data starts at 00:01 UTC; [00:00, 01:00) holds minutes 1..59


def test_upsert_is_idempotent_and_updates(client, auth, src_table):
    pg = add_pg(client, auth)
    dst = src_table + "_dst"
    body = spec(pg, f"public.{src_table}", pg, f"public.{dst}", target={"mode": "upsert", "keys": ["id"]}, options={"split_column": "id"})
    assert run(client, auth, body)["rows_written"] == N
    pg_run(f"update {src_table} set name = 'changed' where id <= 100")
    assert run(client, auth, body)["status"] == "succeeded"
    assert pg_run(f"select count(*), count(*) filter (where name = 'changed') from {dst}")[0] == (N, 100)


def test_replace_empties_target_first(client, auth, src_table):
    pg = add_pg(client, auth)
    dst = src_table + "_dst"
    run(client, auth, spec(pg, f"public.{src_table}", pg, f"public.{dst}"))
    pg_run(f"delete from {src_table} where id > 10")
    j = run(client, auth, spec(pg, f"public.{src_table}", pg, f"public.{dst}", target={"mode": "replace"}))
    assert j["rows_written"] == 10 and pg_run(f"select count(*) from {dst}")[0][0] == 10


def test_bad_rows_are_isolated_and_written_to_rejects_file(client, auth, src_table):
    pg = add_pg(client, auth)
    dst = src_table + "_dst"
    pg_run(f"create table {dst}(id bigint, amount numeric(12,2), name varchar(20), note text, ts timestamptz, d date, "
           f"flag boolean, raw bytea, j text, constraint pos check (id % 1000 <> 7))")  # rejects 7, 1007, 2007 ...
    j = run(client, auth, spec(pg, f"public.{src_table}", pg, f"public.{dst}", options={"split_column": "id"}))
    assert j["status"] == "succeeded", j
    expected_bad = N // 1000 + (1 if N % 1000 >= 7 else 0)
    assert j["rows_rejected"] == expected_bad and j["rows_written"] == N - expected_bad
    assert pg_run(f"select count(*) from {dst}")[0][0] == N - expected_bad
    r = client.get(f"/api/jobs/{j['id']}/rejects", headers=auth)
    assert r.status_code == 200 and r.text.count("\n") >= expected_bad + 1 and "pos" in r.text


def test_refused_everything_is_reported_as_a_target_problem(client, auth, src_table):
    pg = add_pg(client, auth)
    dst = src_table + "_dst"
    pg_run(f"create table {dst}(id bigint, amount numeric(12,2), name varchar(20), note text, ts timestamptz, d date, "
           f"flag boolean, raw bytea, j text, constraint never check (id < 0))")
    j = run(client, auth, spec(pg, f"public.{src_table}", pg, f"public.{dst}", options={"split_column": "id", "workers": 2}))
    assert j["status"] == "failed" and "not bad data" in j["error"]


WRITERS = {"psycopg": "PgWriter", "adbc": "PgAdbcWriter"}


@pytest.mark.parametrize("driver", list(WRITERS))
def test_cancel_then_retry_resumes_without_duplicates(client, auth, src_table, monkeypatch, driver):
    import time as _t

    from app.engine.adapters import pg as pgmod

    cls = getattr(pgmod, WRITERS[driver])
    orig = cls.write
    monkeypatch.setattr(cls, "write", lambda self, t: (_t.sleep(0.4), orig(self, t))[1])  # slow enough that a cancel (noticed within ~1 s) lands midway
    pg = add_pg(client, auth)
    dst = src_table + "_dst"
    r = client.post("/api/jobs", json=spec(pg, f"public.{src_table}", pg, f"public.{dst}",
                                           options={"split_column": "id", "workers": 2, "write_batch": 200, "read_chunk": 200,
                                                    "source_driver": driver, "target_driver": driver}), headers=auth)
    jid = r.json()["id"]
    end = _t.time() + 30
    while _t.time() < end and client.get(f"/api/jobs/{jid}", headers=auth).json()["rows_written"] < 800:
        _t.sleep(0.1)
    assert client.post(f"/api/jobs/{jid}/cancel", headers=auth).status_code == 200
    j = wait_job(client, auth, jid)
    assert j["status"] == "cancelled" and 0 < j["rows_written"] < N
    monkeypatch.setattr(cls, "write", orig)
    assert client.post(f"/api/jobs/{jid}/retry", headers=auth).status_code == 200
    j = wait_job(client, auth, jid)
    assert j["status"] == "succeeded", j
    assert pg_run(f"select count(*), count(distinct id) from {dst}")[0] == (N, N)  # nothing lost, nothing twice
    assert _checksum(src_table) == _checksum(dst)


@pytest.mark.parametrize("driver", list(WRITERS))
def test_transient_failure_retries_from_last_committed_row(client, auth, src_table, monkeypatch, driver):
    import psycopg

    from app.engine.adapters import pg as pgmod

    cls = getattr(pgmod, WRITERS[driver])
    orig, calls = cls.write, {"n": 0}

    def flaky(self, t):
        calls["n"] += 1
        if calls["n"] == 6:
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        return orig(self, t)

    monkeypatch.setattr(cls, "write", flaky)
    pg = add_pg(client, auth)
    dst = src_table + "_dst"
    j = run(client, auth, spec(pg, f"public.{src_table}", pg, f"public.{dst}", options={
        "split_column": "id", "workers": 2, "write_batch": 300, "source_driver": driver, "target_driver": driver}))
    assert j["status"] == "succeeded", j
    assert pg_run(f"select count(*), count(distinct id) from {dst}")[0] == (N, N)
    assert any("retry 1/3" in line["msg"] for line in client.get(f"/api/jobs/{j['id']}/log?n=200", headers=auth).json())


def test_watermark_loads_only_new_rows(client, auth, src_table):
    pg = add_pg(client, auth)
    dst = src_table + "_dst"
    body = spec(pg, f"public.{src_table}", pg, f"public.{dst}", options={"strategy": "watermark", "split_column": "id"})
    assert run(client, auth, body)["rows_written"] == N
    pg_run(f"insert into {src_table}(id, amount, name, ts, d, flag, raw, j) select g, 1, 'new', now(), now(), true, '\\x00', '{{}}' "
           f"from generate_series({N + 1}, {N + 50}) g")
    j = run(client, auth, body)
    assert j["rows_written"] == 50 and pg_run(f"select count(*) from {dst}")[0][0] == N + 50
    assert run(client, auth, body)["rows_written"] == 0  # nothing new


def test_plan_reports_ddl_splits_and_a_timed_sample(client, auth, src_table):
    pg = add_pg(client, auth)
    r = client.post("/api/jobs/plan", json=spec(pg, f"public.{src_table}", pg, f"public.{src_table}_dst",
                                                options={"split_column": "id", "workers": 3}), headers=auth)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["rows_total"] == N and p["rows_total_exact"] is False or p["rows_total"] == N
    assert len(p["splits"]) == 6 and p["workers"] == 3 and p["split_unique"] is True
    assert p["ddl"][-1].startswith("CREATE TABLE") and p["sample"]["rows"] > 0 and len(p["sample"]["preview"]) > 0
    assert pg_run(f"select to_regclass('public.{src_table}_dst')")[0][0] is None  # a plan writes nothing


def test_bad_requests_fail_fast_with_readable_errors(client, auth, src_table):
    pg = add_pg(client, auth)
    for body, needle in [
        (spec(pg, "public.nope_" + uniq(), pg, "public.x"), "not found"),
        (spec(pg, f"public.{src_table}", pg, "public.x", options={"strategy": "id", "split_column": "name"}), "numeric"),
        (spec(pg, f"public.{src_table}", pg, "public.x", target={"mode": "upsert", "keys": ["nope"]}), "Key column"),
        (spec(pg, f"public.{src_table}", pg, "public.x", columns=[{"source": "ghost"}]), "ghost"),
    ]:
        r = client.post("/api/jobs", json=body, headers=auth)
        assert r.status_code == 422 and needle in r.text, (needle, r.text)
    pg_run(f"create table {src_table}_dst(id bigint)")
    r = client.post("/api/jobs", json=spec(pg, f"public.{src_table}", pg, f"public.{src_table}_dst"), headers=auth)
    assert r.status_code == 422 and "does not exist in target table" in r.text


def test_column_mapping_rename_cast_and_exclude(client, auth, src_table):
    pg = add_pg(client, auth)
    dst = src_table + "_dst"
    cols = [{"source": "id"}, {"source": "name", "name": "label", "type": "VARCHAR(30)"}, {"source": "amount", "type": "DOUBLE"},
            {"source": "d", "type": "TIMESTAMP"}, {"source": "note", "include": False}]
    j = run(client, auth, spec(pg, f"public.{src_table}", pg, f"public.{dst}", columns=cols))
    assert j["status"] == "succeeded", j
    got = pg_run("select column_name, data_type from information_schema.columns where table_name = %s order by ordinal_position", [dst])
    assert got == [("id", "bigint"), ("label", "character varying"), ("amount", "double precision"), ("d", "timestamp without time zone")]


def test_events_stream_ends_with_done(client, auth, src_table):
    pg = add_pg(client, auth)
    r = client.post("/api/jobs", json=spec(pg, f"public.{src_table}", pg, f"public.{src_table}_dst", options={"split_column": "id"}), headers=auth)
    jid, token = r.json()["id"], auth["Authorization"].split()[1]
    assert client.get(f"/api/jobs/{jid}/events").status_code == 401
    with client.stream("GET", f"/api/jobs/{jid}/events?token={token}") as s:
        body = "".join(s.iter_text())
    assert "event: job" in body and "event: done" in body and '"status": "succeeded"' in body


def test_only_the_latest_finished_jobs_are_kept(client, auth, src_table, monkeypatch):
    monkeypatch.setenv("CONDUIT_KEEP_FINISHED_JOBS", "2")
    from app.config import get_settings

    get_settings.cache_clear()
    pg = add_pg(client, auth)
    ids = [run(client, auth, spec(pg, f"public.{src_table}", pg, f"public.{src_table}_dst", options={"strategy": "id", "split_column": "id",
                                                                                                     "lo": 1, "hi": 5}))["id"]
           for _ in range(4)]
    kept = [j["id"] for j in client.get("/api/jobs", headers=auth).json()]
    assert kept == ids[:1:-1] or set(kept) == set(ids[2:])
