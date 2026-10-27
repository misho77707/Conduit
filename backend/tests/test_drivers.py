"""Driver options: the connection setting, the choice per job side, fallback rules, mid-run switches.

No external services: SQLite stands in for a database with two drivers ("fast" and the classic "sqlite3"), and the
fast one's capability checks and failures are faked.
"""
import sqlite3

import pytest

from app.connectors.base import DriverOption
from app.engine.drivers import CapabilityError
from jobhelpers import wait_job

FAST = DriverOption("fast", "Fast (test)", "a fake fast driver")
CLASSIC = DriverOption("sqlite3", "sqlite3 (classic)", "the classic driver", classic=True)


@pytest.fixture()
def fake(monkeypatch):
    """SQLite gets two drivers. state: problem (what driver_problem says about "fast"), fail (the fast reader raises
    a CapabilityError "before" its first batch or "after" it)."""
    from app.connectors.sqlite_db import SQLiteConnector
    from app.engine.adapters.sqlite_db import SQLiteAdapter

    state = {"problem": None, "fail": None}
    monkeypatch.setattr(SQLiteConnector, "drivers", (FAST, CLASSIC))
    monkeypatch.setattr(SQLiteAdapter, "driver_problem",
                        lambda self, d, side, ctx: state["problem"] if d == "fast" and side == "source" else None)
    orig = SQLiteAdapter.read_batches

    def read(self, req, split, n):
        batches = orig(self, req, split, n)
        if self.driver_for("source") == "fast" and state["fail"] == "before":
            raise CapabilityError("source", "the fast driver cannot convert column x")
        for i, b in enumerate(batches):
            yield b
            if self.driver_for("source") == "fast" and state["fail"] == "after" and i == 0:
                raise CapabilityError("source", "the fast driver cannot convert column x")

    monkeypatch.setattr(SQLiteAdapter, "read_batches", read)
    return state


def _db(env, name, rows=0):
    p = env / name
    c = sqlite3.connect(p)
    c.execute("create table t (id integer primary key, v text)")
    c.executemany("insert into t values (?, ?)", [(i, f"v{i}") for i in range(1, rows + 1)])
    c.commit()
    c.close()
    return p


def _conn(client, auth, path, **kw):
    r = client.post("/api/connections", json={"name": path.stem, "type": "sqlite", "config": {"path": str(path), "create_if_missing": True}, **kw},
                    headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def _job(src, dst, **options):
    return {"source": {"connection_id": src["id"], "table": "t"},
            "target": {"connection_id": dst["id"], "table": "t2", "mode": "append"},
            "options": {"workers": 1, "read_chunk": 100, "write_batch": 100, **options}}


def _count(path, table="t2"):
    return sqlite3.connect(path).execute(f"select count(*), count(distinct id) from {table}").fetchone()


# ---- the connection setting ----------------------------------------------------------------------------------

def test_connection_driver_defaults_and_validation(client, auth, env):
    types = {t["type"]: t for t in client.get("/api/meta/db-types", headers=auth).json()}
    assert [d["id"] for d in types["postgres"]["drivers"]] == ["adbc", "psycopg"]
    assert types["postgres"]["drivers"][1]["classic"] and types["sqlite"]["drivers"] == []
    pg = client.post("/api/connections", json={"name": "pg", "type": "postgres", "config": {"host": "127.0.0.1", "port": 1, "database": "d", "user": "u"},
                                               "secrets": {"password": "x"}}, headers=auth).json()
    assert pg["driver"] == "auto"  # new connections: the fastest available, with fallback
    lite = _conn(client, auth, env / "a.sqlite")
    assert lite["driver"] is None  # one driver only: nothing to choose
    r = client.patch(f"/api/connections/{pg['id']}", json={"driver": "psycopg"}, headers=auth)
    assert r.status_code == 200 and r.json()["driver"] == "psycopg"
    r = client.patch(f"/api/connections/{pg['id']}", json={"driver": "turbo"}, headers=auth)
    assert r.status_code == 422 and "auto or one of: adbc, psycopg" in r.json()["detail"]
    r = client.patch(f"/api/connections/{lite['id']}", json={"driver": "adbc"}, headers=auth)
    assert r.status_code == 422 and "one driver only" in r.json()["detail"]
    r = client.post("/api/connections", json={"name": "pg2", "type": "postgres", "driver": "psycopg",
                                               "config": {"host": "h", "database": "d", "user": "u"}, "secrets": {"password": "x"}}, headers=auth)
    assert r.json()["driver"] == "psycopg"


def test_the_migration_keeps_existing_connections_on_their_driver(tmp_path):
    import sqlalchemy as sa
    from alembic import command

    from app.db import Base
    from test_migrations import _alembic, _id, _insert

    url = f"sqlite:///{tmp_path / 'm.db'}"
    cfg = _alembic(url)
    eng = sa.create_engine(url)
    Base.metadata.create_all(eng)  # today's tables, taken back to 0011 (older migrations can't run on SQLite)
    command.stamp(cfg, "head")
    command.downgrade(cfg, "0011")
    with eng.begin() as c:
        for typ in ("postgres", "mysql", "sqlite"):
            _insert(c, "connections", id=_id(eng), name=typ, type=typ)
    command.upgrade(cfg, "head")
    with eng.connect() as c:
        got = dict(c.execute(sa.text("select type, driver from connections")).all())
    assert got == {"postgres": "psycopg", "mysql": "pymysql", "sqlite": None}


# ---- the choice per side -------------------------------------------------------------------------------------

def test_auto_uses_the_fast_driver_and_the_plan_says_so(client, auth, env, fake):
    src, dst = _conn(client, auth, _db(env, "s.sqlite", 50)), _conn(client, auth, env / "d.sqlite")
    assert src["driver"] == "auto"
    plan = client.post("/api/jobs/plan", json=_job(src, dst), headers=auth).json()
    assert plan["drivers"]["source"] == {"requested": "auto", "used": "fast", "label": "Fast (test)", "explicit": False, "why": None}
    assert plan["drivers"]["target"]["used"] == "fast" and plan["sample"]["rows"] == 50  # the sample read with that driver


def test_auto_falls_back_with_the_exact_reason(client, auth, env, fake):
    fake["problem"] = "column `tags` is `text[]`, which the fast read path here doesn't convert"
    src, dst = _conn(client, auth, _db(env, "s.sqlite", 50)), _conn(client, auth, env / "d.sqlite")
    plan = client.post("/api/jobs/plan", json=_job(src, dst), headers=auth).json()
    why = "source: using sqlite3 (classic) — column `tags` is `text[]`, which the fast read path here doesn't convert"
    assert plan["drivers"]["source"]["used"] == "sqlite3" and plan["drivers"]["source"]["why"] == why
    assert why in plan["notes"] and plan["drivers"]["target"]["used"] == "fast"  # each side decides for itself
    j = wait_job(client, auth, client.post("/api/jobs", json=_job(src, dst), headers=auth).json()["id"])
    assert j["status"] == "succeeded" and why in j["plan"]["notes"] and j["plan"]["drivers"]["source"]["used"] == "sqlite3"


def test_a_missing_package_falls_back_under_auto(client, auth, env, monkeypatch, fake):
    from app.connectors.sqlite_db import SQLiteConnector

    monkeypatch.setattr(SQLiteConnector, "drivers", (DriverOption("fast", "Fast (test)", "x", module="no_such_package_xyz"), CLASSIC))
    src, dst = _conn(client, auth, _db(env, "s.sqlite", 5)), _conn(client, auth, env / "d.sqlite")
    plan = client.post("/api/jobs/plan", json=_job(src, dst), headers=auth).json()
    assert plan["drivers"]["source"]["why"] == "source: using sqlite3 (classic) — the no_such_package_xyz package is not installed here"


def test_an_explicit_driver_fails_instead_of_falling_back(client, auth, env, fake):
    fake["problem"] = "column `tags` is `text[]`, which the fast read path here doesn't convert"
    src, dst = _conn(client, auth, _db(env, "s.sqlite", 5)), _conn(client, auth, env / "d.sqlite")
    for body in (_job(src, dst, source_driver="fast"),):
        for path in ("/api/jobs/plan", "/api/jobs"):
            r = client.post(path, json=body, headers=auth)
            assert r.status_code == 422 and r.json()["detail"] == (
                "source driver Fast (test) can't be used: column `tags` is `text[]`, which the fast read path here doesn't convert"), r.text
    client.patch(f"/api/connections/{src['id']}", json={"driver": "fast"}, headers=auth)  # explicit on the connection: the same
    assert client.post("/api/jobs/plan", json=_job(src, dst), headers=auth).status_code == 422
    r = client.post("/api/jobs/plan", json=_job(src, dst, source_driver="nope"), headers=auth)
    assert r.status_code == 422 and "not one of: auto, fast, sqlite3" in r.json()["detail"]


def test_the_per_job_override(client, auth, env, fake):
    src, dst = _conn(client, auth, _db(env, "s.sqlite", 5)), _conn(client, auth, env / "d.sqlite")
    plan = client.post("/api/jobs/plan", json=_job(src, dst, source_driver="sqlite3"), headers=auth).json()
    assert plan["drivers"]["source"] == {"requested": "sqlite3", "used": "sqlite3", "label": "sqlite3 (classic)", "explicit": True, "why": None}
    assert plan["drivers"]["target"]["used"] == "fast"  # the other side keeps the connection's setting
    client.patch(f"/api/connections/{src['id']}", json={"driver": "sqlite3"}, headers=auth)
    assert client.post("/api/jobs/plan", json=_job(src, dst), headers=auth).json()["drivers"]["source"]["used"] == "sqlite3"
    assert client.post("/api/jobs/plan", json=_job(src, dst, source_driver="auto"), headers=auth).json()["drivers"]["source"]["used"] == "fast"


# ---- mid-run capability errors -------------------------------------------------------------------------------

def _log(client, auth, jid):
    return [x["msg"] for x in client.get(f"/api/jobs/{jid}/log?n=200", headers=auth).json()]


def test_a_capability_error_before_anything_is_written_switches_that_split(client, auth, env, fake):
    fake["fail"] = "before"
    src, dst = _conn(client, auth, _db(env, "s.sqlite", 250)), _conn(client, auth, env / "d.sqlite")
    j = wait_job(client, auth, client.post("/api/jobs", json=_job(src, dst), headers=auth).json()["id"])
    assert j["status"] == "succeeded", j
    assert _count(env / "d.sqlite") == (250, 250)
    assert any("Fast (test) stopped" in m and "retrying this split with sqlite3 (classic)" in m for m in _log(client, auth, j["id"]))


def test_it_never_switches_after_rows_were_written_unless_the_split_resumes_exactly(client, auth, env, fake):
    fake["fail"] = "after"
    src, dst = _conn(client, auth, _db(env, "s.sqlite", 250)), _conn(client, auth, env / "d.sqlite")
    j = wait_job(client, auth, client.post("/api/jobs", json=_job(src, dst), headers=auth).json()["id"])
    assert j["status"] == "failed" and "cannot convert column x" in j["error"]  # whole-table split: no exact resume
    assert not any("retrying this split" in m for m in _log(client, auth, j["id"]))

    dst2 = _conn(client, auth, env / "d2.sqlite")  # split on the unique id: resumes after the last committed key
    j = wait_job(client, auth, client.post("/api/jobs", json=_job(src, dst2, split_column="id"), headers=auth).json()["id"])
    assert j["status"] == "succeeded", j
    assert _count(env / "d2.sqlite") == (250, 250)  # nothing twice
    assert any("retrying this split with sqlite3 (classic) from the last committed row" in m for m in _log(client, auth, j["id"]))


def test_an_explicit_driver_never_switches(client, auth, env, fake):
    fake["fail"] = "before"
    src, dst = _conn(client, auth, _db(env, "s.sqlite", 50)), _conn(client, auth, env / "d.sqlite")
    j = wait_job(client, auth, client.post("/api/jobs", json=_job(src, dst, source_driver="fast"), headers=auth).json()["id"])
    assert j["status"] == "failed" and "cannot convert column x" in j["error"]
