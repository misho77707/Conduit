import socket

import pytest

from app.connectors import get_connector
from jobhelpers import CH_HOST, MY_HOST, PG_HOST

from app.connectors.base import Connector, TableSchema, parse_extra, redact
from app.connectors.mongo import mask_uri


def test_mask_uri_strips_credentials_and_query():
    assert mask_uri("mongodb+srv://u:p%40ss@c0.x.mongodb.net/db?retryWrites=true") == "mongodb+srv://c0.x.mongodb.net/db"
    assert mask_uri("mongodb://u:p@h1:27017,h2:27017/") == "mongodb://h1:27017,h2:27017"
    assert mask_uri("mongodb://u:p@ss@h1/db?x=1") == "mongodb://h1/db"  # '@' inside the password
    assert mask_uri("not a uri") == "mongodb://…"


def test_redact_and_parse_extra():
    assert redact("auth failed for pw=abc123xyz", {"password": "abc123xyz"}) == "auth failed for pw=***"
    assert redact("short", {"password": "ab"}) == "short"  # too short to safely replace
    assert parse_extra("connect_timeout=10\n# c\napplication_name=conduit\n\nbad") == {
        "connect_timeout": "10", "application_name": "conduit"}
    assert parse_extra(None) == {}


def test_slow_handshake_is_degraded(env, monkeypatch):
    class Slow(Connector):
        type, label, abbr, kind = "slow", "Slow", "SL", "host"
        def _test(self):
            return "v1", "ok"
        def list_tables(self):
            return []
        def inspect(self, table):
            return TableSchema(name=table, columns=[])

    import time
    from app.config import get_settings

    ticks = iter([0.0, (get_settings().connection_slow_ms + 100) / 1000])
    monkeypatch.setattr(time, "perf_counter", lambda: next(ticks, 99.0))
    r = Slow({}, {}).test()
    assert r.ok and r.degraded and r.status == "warn"


def test_driver_exception_becomes_error_result():
    class Boom(Connector):
        type, label, abbr, kind = "boom", "Boom", "BM", "host"
        def _test(self):
            raise RuntimeError("bad password hunter22")
        def list_tables(self):
            return []
        def inspect(self, table):
            return TableSchema(name=table, columns=[])

    r = Boom({}, {"password": "hunter22"}).test()
    assert not r.ok and r.status == "err" and "hunter22" not in r.error and "***" in r.error


def test_sqlite_inspect_rowid_and_index_candidates(env):
    import sqlite3
    p = env / "s.sqlite"
    con = sqlite3.connect(p)
    con.execute("create table events(id integer primary key, ts timestamp, payload text)")
    con.execute("create index idx_ts on events(ts)")
    con.executemany("insert into events(ts, payload) values (?, ?)", [("2024-01-01", "x"), ("2024-01-02", "y")])
    con.commit()
    con.close()

    schema = get_connector("sqlite", {"path": str(p)}, {}).inspect("main.events")
    assert {c.name for c in schema.columns} == {"id", "ts", "payload"}
    by_col = {c.column: c for c in schema.candidates}
    assert by_col["id"].verdict == "recommended" and by_col["id"].mode == "id_range"
    assert by_col["ts"].verdict == "alternative" and by_col["ts"].mode == "time_range"
    assert schema.row_estimate == 2 and schema.exact


def test_sqlite_inspect_falls_back_to_rowid_without_explicit_pk(env):
    import sqlite3
    p = env / "s2.sqlite"
    con = sqlite3.connect(p)
    con.execute("create table plain(name text)")
    con.commit()
    con.close()

    schema = get_connector("sqlite", {"path": str(p)}, {}).inspect("main.plain")
    assert len(schema.candidates) == 1
    assert schema.candidates[0].column == "rowid" and schema.candidates[0].verdict == "recommended"


def test_duckdb_inspect_with_and_without_pk(env):
    import duckdb as ddb
    p = env / "w.duckdb"
    con = ddb.connect(str(p))
    con.execute("create table keyed(id integer primary key, name varchar)")
    con.execute("create table unkeyed(name varchar)")
    con.close()

    conn = get_connector("duckdb", {"path": str(p)}, {})
    keyed = conn.inspect("main.keyed")
    assert keyed.candidates and keyed.candidates[0].column == "id" and keyed.candidates[0].mode == "id_range"

    unkeyed = conn.inspect("main.unkeyed")
    assert unkeyed.candidates == [] and unkeyed.notes


def _pg_up() -> bool:
    try:
        socket.create_connection((PG_HOST, 5432), timeout=1).close()
        return True
    except OSError:
        return False


def _mysql_up() -> bool:
    try:
        socket.create_connection((MY_HOST, 3306), timeout=1).close()
        return True
    except OSError:
        return False


def _ch_up() -> bool:
    try:
        socket.create_connection((CH_HOST, 8123), timeout=1).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _mysql_up(), reason="needs a local MySQL/MariaDB on :3306 (conduit/conduit, matching jobhelpers.py)")
def test_mysql_connector_lists_every_accessible_schema():
    """list_tables() must not be pinned to config['database'] - it should see every schema the user can.

    Reuses the same 'conduit'/'conduit' account the rest of the suite expects (see jobhelpers.py's MY /
    needs_mysql), and adds a second schema, 'conduit_zz2', that account also has privileges on.
    """
    import pymysql

    admin = pymysql.connect(host=MY_HOST, port=3306, user="conduit", password="conduit", autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute("CREATE DATABASE IF NOT EXISTS conduit_zz2")
            cur.execute("CREATE TABLE IF NOT EXISTS conduit.zz_conn_test(id bigint primary key)")
            cur.execute("CREATE TABLE IF NOT EXISTS conduit_zz2.zz_conn_test2(id bigint primary key)")
    finally:
        admin.close()
    try:
        # `database` is pinned to just one of the two schemas, but both must still be listed: the
        # connection's `database` narrows the driver's default schema, not what the user can see.
        cfg = {"host": MY_HOST, "port": 3306, "user": "conduit", "database": "conduit"}
        names = {t.name for t in get_connector("mysql", cfg, {"password": "conduit"}).list_tables()}
        assert "conduit.zz_conn_test" in names and "conduit_zz2.zz_conn_test2" in names
    finally:
        admin = pymysql.connect(host=MY_HOST, port=3306, user="conduit", password="conduit", autocommit=True)
        with admin.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS conduit.zz_conn_test")
            cur.execute("DROP DATABASE IF EXISTS conduit_zz2")
        admin.close()


@pytest.mark.skipif(not _ch_up(), reason="needs a local ClickHouse on :8123 (default user, database conduit, matching jobhelpers.py)")
def test_clickhouse_connector_lists_every_accessible_database():
    """list_tables() must not be pinned to config['database'] - it should see every database the user can."""
    import clickhouse_connect

    admin = clickhouse_connect.get_client(host=CH_HOST, port=8123)
    try:
        admin.command("CREATE DATABASE IF NOT EXISTS conduit_zz2")
        admin.command("CREATE TABLE IF NOT EXISTS conduit.zz_conn_test(id UInt64) ENGINE=MergeTree ORDER BY id")
        admin.command("CREATE TABLE IF NOT EXISTS conduit_zz2.zz_conn_test2(id UInt64) ENGINE=MergeTree ORDER BY id")
    finally:
        admin.close()
    try:
        cfg = {"host": CH_HOST, "port": 8123, "database": "conduit"}
        names = {t.name for t in get_connector("clickhouse", cfg, {}).list_tables()}
        assert "conduit.zz_conn_test" in names and "conduit_zz2.zz_conn_test2" in names
    finally:
        admin = clickhouse_connect.get_client(host=CH_HOST, port=8123)
        admin.command("DROP TABLE IF EXISTS conduit.zz_conn_test")
        admin.command("DROP DATABASE IF EXISTS conduit_zz2")
        admin.close()


def test_mongo_connector_lists_every_accessible_database(monkeypatch):
    """list_tables() must enumerate every database the connection can see, not just one resolved default.

    Uses mongomock (an in-memory stand-in), matching the rest of the suite's approach to MongoDB (see
    test_jobs_more.py's `mongo` fixture) - a real MongoDB server has never been part of this project's
    live-test setup (see README's "Tested against" notes).
    """
    mongomock = pytest.importorskip("mongomock")
    import app.connectors.mongo as cm

    shared = mongomock.MongoClient()
    shared.close = lambda: None
    monkeypatch.setattr(cm, "MongoClient", lambda *a, **k: shared)
    shared.conduit_zz1.zz_a.insert_one({"x": 1})
    shared.conduit_zz2.zz_b.insert_one({"x": 1})

    # No `database` in the config or the URI path: previously this raised "No database set". Now it
    # should enumerate every database the connection can see.
    names = {t.name for t in get_connector("mongodb", {}, {"uri": "mongodb://localhost"}).list_tables()}
    assert "conduit_zz1.zz_a" in names and "conduit_zz2.zz_b" in names


@pytest.mark.skipif(not _pg_up(), reason="needs a local PostgreSQL on :5432 (conduit/conduit)")
def test_postgres_connector_against_real_server():
    import psycopg

    cfg = {"host": PG_HOST, "port": 5432, "database": "conduit", "user": "conduit"}
    with psycopg.connect(f"host={PG_HOST} dbname=conduit user=conduit password=conduit", autocommit=True) as c:
        c.execute("drop table if exists public.zz_conn_test")
        c.execute("create table public.zz_conn_test(id bigint primary key, ts timestamptz)")
        c.execute("insert into public.zz_conn_test select g, now() from generate_series(1, 500) g")
        c.execute("analyze public.zz_conn_test")
    try:
        conn = get_connector("postgres", cfg, {"password": "conduit"})
        r = conn.test()
        assert r.ok, r.error
        assert r.version.startswith("PostgreSQL 16") and "user conduit" in r.detail
        t = {x.name: x for x in conn.list_tables()}["public.zz_conn_test"]
        assert t.est_rows == 500 and t.size_bytes and t.size_bytes > 0

        schema = conn.inspect("public.zz_conn_test")
        by_col = {c.column: c for c in schema.candidates}
        assert by_col["id"].verdict == "recommended" and by_col["id"].mode == "id_range"
        assert schema.row_estimate == 500

        bad = get_connector("postgres", cfg, {"password": "wrong-password"}).test()
        assert not bad.ok and "wrong-password" not in bad.error
    finally:
        with psycopg.connect(f"host={PG_HOST} dbname=conduit user=conduit password=conduit", autocommit=True) as c:
            c.execute("drop table if exists public.zz_conn_test")
