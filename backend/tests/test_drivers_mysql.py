"""MySQL drivers: pymysql (classic) and "fast" (connectorx reads + LOAD DATA writes). The checks and the CSV need no
server; the rest runs against a live MySQL / MariaDB (skipped without one)."""
import datetime as dt
import decimal

import pyarrow as pa
import pymysql
import pytest

from jobhelpers import MY, add_my, needs_mysql, run, spec, uniq


# ---- no server -------------------------------------------------------------------------------------------------

class _Conn:
    def __init__(self, ssl="disable"):
        self.config = {"host": "h", "port": 3306, "database": "d", "user": "u", "ssl": ssl}
        self.secrets = {"password": "p@ss/w"}

    def cx_uri(self):
        from app.connectors.mysql_db import MySQLConnector

        return MySQLConnector.cx_uri(self)


def _adapter(conn=None):
    from app.engine.adapters.mysql_db import MySQLAdapter

    return MySQLAdapter(conn or _Conn())


def test_the_fast_drivers_checks():
    from app.engine.model import ReadRequest
    from app.engine.types import ColSpec

    a = _adapter()
    a._rows = lambda sql, params=None: [("local_infile", "OFF")]
    assert a.driver_problem("fast", "target", None) == "LOAD DATA needs local_infile=ON on the server (it is OFF)"
    a._rows = lambda sql, params=None: [("local_infile", "ON")]
    assert a.driver_problem("fast", "target", None) is None
    req = ReadRequest(table="d.t", columns=[ColSpec("a", "INTEGER"), ColSpec("b", "VARCHAR")])
    assert _adapter(_Conn("require")).driver_problem("fast", "source", req) == "connectorx can't use this connection's TLS setting"
    assert a.driver_problem("fast", "source", ReadRequest(query="select 1", columns=req.columns)).startswith("custom SQL may depend")
    a._cx_plan = lambda r: {"a": ("direct", "int"), "b": (None, "time(3)")}
    assert a.driver_problem("fast", "source", req) == "column `b` is `time(3)`, which the connectorx read path here doesn't convert"
    assert _Conn().cx_uri() == "mysql://u:p%40ss%2Fw@h:3306/d"  # credentials escaped in the URI


def test_load_data_text_is_built_without_python_per_row():
    from app.engine.adapters.mysql_db import load_data_text

    t = pa.table({
        "i": pa.array([1, None], pa.int32()),
        "s": pa.array(['a,"b"\nc', None]),
        "e": pa.array(["", "NULL"]),
        "b": pa.array([True, None]),
        "d": pa.array([decimal.Decimal("1.50"), None], pa.decimal128(12, 2)),
        "tz": pa.array([dt.datetime(2024, 1, 1, 12, tzinfo=dt.timezone.utc), None], pa.timestamp("us", "UTC")),
        "x": pa.array([b"\xde\xad", None]),
    })
    assert load_data_text(t) == ('1,"a,""b""\nc","",1,1.50,2024-01-01 12:00:00.000000,dead\n'
                                 'NULL,NULL,"NULL",NULL,NULL,NULL,NULL\n').encode()


# ---- a live server ---------------------------------------------------------------------------------------------

def my_run(sql, params=None):
    c = pymysql.connect(host=MY["host"], port=MY["port"], user="conduit", password="conduit", database="conduit",
                        autocommit=True, init_command="SET time_zone = '+00:00'")
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        c.close()


@pytest.fixture()
def typed():
    t = uniq("typed")
    my_run(f"CREATE TABLE {t} (id BIGINT PRIMARY KEY, ti TINYINT, b BOOLEAN, si SMALLINT, mi MEDIUMINT, i INT, ui INT UNSIGNED, "
           f"ubi BIGINT UNSIGNED, f FLOAT, d DOUBLE, n DECIMAL(12,2), n10 DECIMAL(38,10), dt DATETIME(6), ts TIMESTAMP(6) NULL, "
           f"dd DATE, yr YEAR, vc VARCHAR(30), tx TEXT, ch CHAR(5), bl BLOB, vb VARBINARY(10), js JSON, en ENUM('a','b'), "
           f"st SET('x','y'))")
    for i in range(1, 9):
        my_run(f"INSERT INTO {t} VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
               [i, -i, i % 2, 300 * i, 70000 * i, -7 * i, 4_000_000_000 + i, 18_000_000_000_000_000_000 + i, 1.1 * i, 2.25 / i,
                decimal.Decimal(f"{i}.50"), decimal.Decimal(f"{i}.1234567891"), dt.datetime(2024, 1, i, 12, 0, 0, 123456),
                dt.datetime(2024, 6, i, 8, 0, 0, 5), dt.date(2024, 2, 20 + i), 2000 + i, f'héllo "{i}", x\ny', "long ü 😀 " * i,
                f"c{i}", bytes([i, 255]), bytes([0, i]), '{"b": [1, 2], "a": 1.50, "s": "ü"}', "ab"[i % 2], "x,y" if i % 2 else "y"])
    my_run(f"INSERT INTO {t} (id) VALUES (100)")
    yield t
    my_run(f"DROP TABLE IF EXISTS {t}")


def _read_all(driver, table):
    from app.connectors import get_connector
    from app.engine.adapters import get_adapter
    from app.engine.model import ReadRequest, SplitSpec

    a = get_adapter(get_connector("mysql", MY, {"password": "conduit"}))
    a.use_driver("source", driver)
    req = ReadRequest(table=f"conduit.{table}", columns=a.table_columns(f"conduit.{table}"))
    batches = list(a.read_batches(req, SplitSpec(0, "all", "full"), 3))
    return pa.concat_tables([b.table for b in batches]).sort_by("id"), [r for b in batches for r in b.rejects]


@needs_mysql
def test_both_drivers_read_identical_batches(typed):
    classic, rej_c = _read_all("pymysql", typed)
    fast, rej_f = _read_all("fast", typed)
    assert classic.schema == fast.schema
    for name in classic.column_names:
        assert classic.column(name).to_pylist() == fast.column(name).to_pylist(), name
    assert classic.equals(fast) and rej_c == rej_f == []


@needs_mysql
def test_timestamps_do_not_depend_on_the_servers_time_zone(typed):
    try:
        my_run("SET GLOBAL time_zone = '+04:00'")  # connectorx can't SET time_zone: UNIX_TIMESTAMP keeps it UTC
    except pymysql.err.OperationalError:
        pytest.skip("needs SYSTEM_VARIABLES_ADMIN to change the server's time zone")
    try:
        assert _read_all("fast", typed)[0].column("ts").equals(_read_all("pymysql", typed)[0].column("ts"))
    finally:
        my_run("SET GLOBAL time_zone = 'SYSTEM'")


@needs_mysql
def test_types_connectorx_cannot_read_keep_the_source_on_pymysql(client, auth):
    t = uniq("odd")
    my_run(f"CREATE TABLE {t} (id INT PRIMARY KEY, dur TIME, flags BIT(8))")
    my_run(f"INSERT INTO {t} VALUES (1, '25:00:00', b'101')")
    try:
        my = add_my(client, auth)
        plan = client.post("/api/jobs/plan", json=spec(my, f"conduit.{t}", my, f"conduit.{t}_dst"), headers=auth).json()
        why = "source: using PyMySQL (classic) — column `dur` is `time`, which the connectorx read path here doesn't convert"
        assert plan["drivers"]["source"]["used"] == "pymysql" and why in plan["notes"], plan
        r = client.post("/api/jobs/plan", json=spec(my, f"conduit.{t}", my, f"conduit.{t}_dst", options={"source_driver": "fast"}), headers=auth)
        assert r.status_code == 422 and "can't be used: column `dur` is `time`" in r.json()["detail"]
    finally:
        my_run(f"DROP TABLE IF EXISTS {t}")


@needs_mysql
def test_a_zero_date_moves_that_split_to_pymysql(client, auth):
    """connectorx panics on '0000-00-00': the split (nothing written yet) retries on pymysql, which rejects the row."""
    t = uniq("zd")
    c = pymysql.connect(host=MY["host"], port=MY["port"], user="conduit", password="conduit", database="conduit", autocommit=True)
    with c.cursor() as cur:
        cur.execute("SET SESSION sql_mode = ''")
        cur.execute(f"CREATE TABLE {t} (id INT PRIMARY KEY, d DATETIME)")
        cur.execute(f"INSERT INTO {t} VALUES (1, '2024-01-01'), (2, '0000-00-00 00:00:00'), (3, '2024-01-03')")
    c.close()
    try:
        my = add_my(client, auth)
        j = run(client, auth, spec(my, f"conduit.{t}", my, f"conduit.{t}_dst"))
        assert j["status"] == "succeeded" and (j["rows_written"], j["rows_rejected"]) == (2, 1), j
        log = [x["msg"] for x in client.get(f"/api/jobs/{j['id']}/log?n=200", headers=auth).json()]
        assert any("retrying this split with PyMySQL (classic)" in m for m in log), log
    finally:
        my_run(f"DROP TABLE IF EXISTS {t}")
        my_run(f"DROP TABLE IF EXISTS {t}_dst")


DRIVERS = ["pymysql", "fast"]
COLS = "id, ti, b, si, mi, i, ui, ubi, f, d, n, n10, dt, ts, dd, yr, vc, tx, ch, HEX(bl), HEX(vb), js, en, st"


@needs_mysql
def test_every_driver_loads_append_replace_and_upsert_the_same(client, auth, typed):
    my = add_my(client, auth)
    results = {}
    for sd in DRIVERS:
        for td in DRIVERS:
            dst = f"{typed}_{sd[:2]}{td[:2]}"
            my_run(f"CREATE TABLE {dst} LIKE {typed}")
            try:
                opts = {"split_column": "id", "workers": 2, "read_chunk": 100, "write_batch": 100, "source_driver": sd, "target_driver": td}
                for mode in ("append", "replace"):
                    j = run(client, auth, spec(my, f"conduit.{typed}", my, f"conduit.{dst}", options=opts, target={"mode": mode}))
                    assert j["status"] == "succeeded" and j["rows_rejected"] == 0, j
                    assert j["plan"]["drivers"]["source"]["used"] == sd and j["plan"]["drivers"]["target"]["used"] == td
                my_run(f"UPDATE {dst} SET tx = 'stale' WHERE id <= 3")
                j = run(client, auth, spec(my, f"conduit.{typed}", my, f"conduit.{dst}", options=opts, target={"mode": "upsert", "keys": ["id"]}))
                assert j["status"] == "succeeded", j
                assert my_run(f"SELECT COUNT(*) FROM {dst} WHERE tx = 'stale'")[0][0] == 0
                results[(sd, td)] = my_run(f"SELECT COUNT(*), MD5(GROUP_CONCAT(CONCAT_WS('|', {COLS}) ORDER BY id SEPARATOR ',')) FROM {dst}")
            finally:
                my_run(f"DROP TABLE IF EXISTS {dst}")
    want = my_run(f"SELECT COUNT(*), MD5(GROUP_CONCAT(CONCAT_WS('|', {COLS}) ORDER BY id SEPARATOR ',')) FROM {typed}")
    assert set(map(tuple, results.values())) == {tuple(want)}, (results, want)


@needs_mysql
def test_load_data_warnings_become_rejects(client, auth):
    """LOAD DATA LOCAL only warns about duplicates: the batch is refused and bisected, as an INSERT error would be."""
    src, dst = uniq("ls"), uniq("ld")
    my_run(f"CREATE TABLE {src} (id INT PRIMARY KEY, v VARCHAR(20))")
    my_run(f"INSERT INTO {src} WITH RECURSIVE s(g) AS (SELECT 1 UNION ALL SELECT g + 1 FROM s WHERE g < 300) SELECT g, CONCAT('v', g) FROM s")
    my_run(f"CREATE TABLE {dst} (id INT PRIMARY KEY, v VARCHAR(20))")
    my_run(f"INSERT INTO {dst} VALUES (7, 'already'), (150, 'already'), (299, 'already')")
    try:
        my = add_my(client, auth)
        j = run(client, auth, spec(my, f"conduit.{src}", my, f"conduit.{dst}", options={"target_driver": "fast", "write_batch": 100, "read_chunk": 100}))
        assert j["status"] == "succeeded" and (j["rows_written"], j["rows_rejected"]) == (297, 3), j
        assert "Duplicate entry" in client.get(f"/api/jobs/{j['id']}/rejects", headers=auth).text
        assert my_run(f"SELECT COUNT(*), SUM(v = 'already') FROM {dst}")[0] == (300, 3)
    finally:
        my_run(f"DROP TABLE IF EXISTS {src}")
        my_run(f"DROP TABLE IF EXISTS {dst}")


@needs_mysql
def test_local_infile_off_falls_back_or_refuses(client, auth, typed):
    try:
        my_run("SET GLOBAL local_infile = 0")
    except pymysql.err.OperationalError:
        pytest.skip("needs SYSTEM_VARIABLES_ADMIN to switch local_infile")
    try:
        my = add_my(client, auth)
        body = spec(my, f"conduit.{typed}", my, f"conduit.{typed}_x")
        plan = client.post("/api/jobs/plan", json=body, headers=auth).json()
        why = "target: using PyMySQL (classic) — LOAD DATA needs local_infile=ON on the server (it is OFF)"
        assert plan["drivers"]["target"]["used"] == "pymysql" and why in plan["notes"]
        assert plan["drivers"]["source"]["used"] == "fast"  # reads don't need it
        r = client.post("/api/jobs/plan", json={**body, "options": {**body["options"], "target_driver": "fast"}}, headers=auth)
        assert r.status_code == 422 and "LOAD DATA needs local_infile=ON" in r.json()["detail"]
        j = run(client, auth, body)
        assert j["status"] == "succeeded" and j["rows_written"] == 9
    finally:
        my_run("SET GLOBAL local_infile = 1")
        my_run(f"DROP TABLE IF EXISTS {typed}_x")
