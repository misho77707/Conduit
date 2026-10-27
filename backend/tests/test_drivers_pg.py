"""Postgres drivers against a live server: psycopg (classic) and ADBC give identical batches, and every driver loads
every mode the same way, rejects included."""
import pyarrow as pa
import pytest

from jobhelpers import PG, add_pg, needs_pg, pg_run, run, spec, uniq

pytestmark = needs_pg

JSONB = ['{"a": 1}', '{"b": [1, 2], "a": 1.50}', '{"x": 0.00001}', '{"x": 1e20}', '{"big": 12345678901234567890}',
         '"just a string"', 'true', 'null', '5', '{"ü": "naïve \\"q\\" \\u0001"}', '[1, "two", {"three": 3.0}]']


@pytest.fixture()
def typed():
    """Every common type (and a row of nulls)."""
    t = uniq("typed")
    pg_run("DO $$ BEGIN CREATE TYPE mood AS ENUM ('sad', 'ok', 'happy'); EXCEPTION WHEN duplicate_object THEN NULL; END $$")
    pg_run(f"create table {t} (id bigint primary key, i2 smallint, i4 integer, r real, d double precision, n numeric(12,2), "
           f"nu numeric, b boolean, tx text, vc varchar(10), ch char(5), dt date, ts timestamp, tz timestamptz, by bytea, "
           f"js json, jb jsonb, u uuid, m mood)")
    for i, jb in enumerate(JSONB, start=1):
        pg_run(f"insert into {t} values (%s, %s, %s, 1.1 * %s, 2.25 / %s, %s * 10.05, %s, %s, %s, %s, %s, '2024-02-29'::date + %s, "
               "'2024-01-01 12:00:00.123456'::timestamp + make_interval(hours => %s), "
               "'2024-01-01 12:00:00+04'::timestamptz + make_interval(mins => %s), %s, %s, %s, %s::uuid, 'happy')",
               [i, i, -i * 1000, i, i, i, f"{i}.123456789", i % 2 == 0, f'héllo "{i}"', f"v{i}", f"c{i}", i, i, i,
                bytes.fromhex(f"deadbeef0{i % 10}"), '{"a":  1,"b" : [1,2]}', jb, f"a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a1{i % 10}"])
    pg_run(f"insert into {t} (id) values (100)")
    pg_run(f"insert into {t} (id, nu) values (101, 3.14159265358979323846)")  # more scale than numeric's logical DECIMAL(38,10)
    yield t
    pg_run(f"drop table if exists {t}")


def _adapter(driver):
    from app.connectors import get_connector
    from app.engine.adapters import get_adapter

    a = get_adapter(get_connector("postgres", PG, {"password": "conduit"}))
    a.use_driver("source", driver)
    return a


def _read_all(driver, table):
    from app.engine.model import ReadRequest, SplitSpec

    a = _adapter(driver)
    req = ReadRequest(table=table, columns=a.table_columns(table))
    batches = list(a.read_batches(req, SplitSpec(0, "all", "full"), 4))  # small batches: several per read
    rejects = [(r[1]["id"], r[0].split(" (")[0]) for b in batches for r in b.rejects]
    return pa.concat_tables([b.table for b in batches]).sort_by("id"), sorted(rejects)


def test_both_drivers_read_identical_batches(typed):
    classic, rej_c = _read_all("psycopg", typed)
    fast, rej_f = _read_all("adbc", typed)
    assert classic.schema == fast.schema
    for name in classic.column_names:
        assert classic.column(name).to_pylist() == fast.column(name).to_pylist(), name
    assert classic.equals(fast)
    assert rej_c == rej_f == [("101", "column nu: cannot convert values to decimal128(38, 10)")]  # the same reject, both ways
    jb = dict(zip(classic.column("id").to_pylist(), classic.column("jb").to_pylist()))
    assert jb[6] == "just a string" and jb[7] == "True" and jb[8] is None and jb[2] == '{"a": 1.5, "b": [1, 2]}'


def test_types_adbc_cannot_read_keep_the_source_on_psycopg(client, auth):
    t = uniq("odd")
    pg_run(f"create table {t} (id int primary key, tags text[], span interval, ip inet)")
    pg_run(f"insert into {t} values (1, '{{a,b}}', '1 day', '10.0.0.1')")
    try:
        pg = add_pg(client, auth)
        plan = client.post("/api/jobs/plan", json=spec(pg, f"public.{t}", pg, f"public.{t}_dst"), headers=auth).json()
        why = "source: using psycopg (classic) — column `tags` is `text[]`, which the ADBC read path here doesn't convert"
        assert plan["drivers"]["source"]["used"] == "psycopg" and why in plan["notes"]
        assert plan["drivers"]["target"]["used"] == "adbc"  # writes take every type (through staging)
        r = client.post("/api/jobs/plan", json=spec(pg, f"public.{t}", pg, f"public.{t}_dst", options={"source_driver": "adbc"}), headers=auth)
        assert r.status_code == 422 and "source driver ADBC (Arrow, binary COPY) can't be used: column `tags` is `text[]`" in r.json()["detail"]
        j = run(client, auth, spec(pg, f"public.{t}", pg, f"public.{t}_dst"))
        assert j["status"] == "succeeded" and j["rows_written"] == 1, j
    finally:
        pg_run(f"drop table if exists {t}")
        pg_run(f"drop table if exists {t}_dst")


DRIVERS = ["psycopg", "adbc"]
COLS = "id, i2, i4, r, d, n, nu::numeric(38,10), b, tx, vc, ch, dt, ts, tz, encode(by, 'hex'), u, m"


def _sum(t, cols=COLS + ", js::text, jb::text"):
    return pg_run(f"select count(*), md5(string_agg(concat_ws('|', {cols}), ',' order by id)) from {t}")[0]


def test_every_driver_loads_append_replace_and_upsert_the_same(client, auth, typed):
    """Every source x target driver pair, in every mode: the same rows, the same values (the JSON columns come out
    the way the classic path has always written them, so the target is compared across drivers, not to the source)."""
    pg = add_pg(client, auth)
    keep = "id not in (6, 7, 101)"  # 101: its numeric can't be DECIMAL(38,10); 6 / 7: a top-level jsonb string / true
    # comes out of the classic path as plain text ("just a string", "True"), which a jsonb column refuses
    results = {}
    for sd in DRIVERS:
        for td in DRIVERS:
            dst = f"{typed}_{sd[:2]}{td[:2]}"
            pg_run(f"create table {dst} (like {typed} including all)")  # jsonb / uuid / enum: ADBC writes go through staging
            try:
                opts = {"split_column": "id", "workers": 2, "read_chunk": 100, "write_batch": 100, "source_driver": sd, "target_driver": td}
                for mode in ("append", "replace"):
                    j = run(client, auth, spec(pg, f"public.{typed}", pg, f"public.{dst}", options=opts, target={"mode": mode}))
                    assert j["status"] == "succeeded" and j["rows_rejected"] == 3, j
                    assert j["plan"]["drivers"]["source"]["used"] == sd and j["plan"]["drivers"]["target"]["used"] == td
                pg_run(f"update {dst} set tx = 'stale', jb = '{{}}' where id <= 3")
                j = run(client, auth, spec(pg, f"public.{typed}", pg, f"public.{dst}", options=opts, target={"mode": "upsert", "keys": ["id"]}))
                assert j["status"] == "succeeded", j
                assert pg_run(f"select count(*) from {dst} where tx = 'stale'")[0][0] == 0
                assert _sum(dst, COLS) == pg_run(f"select count(*), md5(string_agg(concat_ws('|', {COLS}), ',' order by id)) "
                                                 f"from {typed} where {keep}")[0]
                results[(sd, td)] = _sum(dst)
            finally:
                pg_run(f"drop table if exists {dst}")
    assert len(set(results.values())) == 1, results  # identical targets whichever drivers moved the rows


@pytest.mark.parametrize("staged", [False, True])
def test_rejects_still_bisect_with_adbc_writes(client, auth, staged):
    """A CHECK constraint refuses some rows: they land in the reject file, the rest is written (both ADBC paths)."""
    src, dst = uniq("rs"), uniq("rd")
    pg_run(f"create table {src} (id int primary key, amount int, j text)")
    pg_run(f"insert into {src} select g, case when g % 50 = 0 then -1 else g end, '{{\"k\": ' || g || '}}' from generate_series(1, 400) g")
    pg_run(f"create table {dst} (id int primary key, amount int check (amount >= 0), j {'jsonb' if staged else 'text'})")
    try:
        pg = add_pg(client, auth)
        j = run(client, auth, spec(pg, f"public.{src}", pg, f"public.{dst}", options={"write_batch": 100, "read_chunk": 100}))
        assert j["status"] == "succeeded" and j["plan"]["drivers"]["target"]["used"] == "adbc", j
        assert (j["rows_written"], j["rows_rejected"]) == (392, 8)
        assert pg_run(f"select count(*), min(amount) from {dst}")[0] == (392, 1)
        assert "check" in client.get(f"/api/jobs/{j['id']}/rejects", headers=auth).text.lower()
    finally:
        pg_run(f"drop table if exists {src}")
        pg_run(f"drop table if exists {dst}")


def test_the_health_check_says_when_the_fast_driver_cannot_open(client, auth, monkeypatch):
    from app.engine.adapters.pg import PostgresAdapter

    pg = add_pg(client, auth)
    r = client.post(f"/api/connections/{pg}/test", headers=auth).json()
    assert r["ok"] and r["note"] is None
    monkeypatch.setattr(PostgresAdapter, "probe_driver", lambda self, d: "no route to the ADBC host")
    r = client.post(f"/api/connections/{pg}/test", headers=auth).json()
    assert r["ok"] and r["status"] == "ok"
    assert r["connection"]["note"] == "ADBC (Arrow, binary COPY) can't connect (no route to the ADBC host); jobs use psycopg (classic)"
    client.patch(f"/api/connections/{pg}", json={"driver": "psycopg"}, headers=auth)
    assert client.post(f"/api/connections/{pg}/test", headers=auth).json()["connection"]["note"] is None
