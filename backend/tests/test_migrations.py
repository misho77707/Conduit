"""0009_roles on an existing single-admin install: SQLite always, PostgreSQL when one is reachable locally."""
import datetime as dt
import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from jobhelpers import PG, needs_pg, uniq

BACKEND = Path(__file__).resolve().parents[1]


def _alembic(url: str) -> Config:
    from app.config import get_settings

    os.environ["CONDUIT_DATABASE_URL"] = url  # alembic/env.py takes the URL from the settings
    get_settings.cache_clear()
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    return cfg


def _id(eng):
    u = uuid.uuid4()
    return u.hex if eng.dialect.name == "sqlite" else u


def _insert(conn, table: str, **values):
    """A row with the given values; every other NOT NULL column without a server default gets a plain filler."""
    t = sa.Table(table, sa.MetaData(), autoload_with=conn)
    row = dict(values)
    for c in t.columns:
        if c.name in row or c.nullable or c.server_default is not None:
            continue
        try:
            py = c.type.python_type
        except NotImplementedError:
            py = str
        row[c.name] = {str: "x", int: 0, float: 0.0, bool: False, dict: {}, list: [],
                       dt.datetime: dt.datetime.now(dt.timezone.utc)}.get(py, f"{c.name}")
    conn.execute(t.insert().values(**row))


def _single_admin_install(url: str, from_base: bool) -> tuple[sa.Engine, Config, object]:
    cfg = _alembic(url)
    if from_base:
        command.upgrade(cfg, "0008")
    else:  # SQLite: the 0004 migration can't run there, so take today's tables back down to 0008
        command.stamp(cfg, "head")
        command.downgrade(cfg, "0008")
    eng = sa.create_engine(url)
    admin = _id(eng)
    with eng.begin() as c:
        _insert(c, "users", id=admin, username="admin", role="admin", is_active=True)
        _insert(c, "connections", id=_id(eng), name="prod-pg", owner_id=admin)
        _insert(c, "jobs", id=_id(eng), name="a load", owner_id=admin)
        _insert(c, "pipelines", id=_id(eng), name="nightly")  # pipelines never recorded an owner
    command.upgrade(cfg, "head")
    return eng, cfg, admin


def _check_upgraded(eng, admin):
    with eng.connect() as c:
        users = c.execute(sa.text("select role, must_change_password, token_version, created_by_id from users")).all()
        assert [(u[0], bool(u[1]), u[2], u[3]) for u in users] == [("superadmin", False, 0, None)]
        assert c.execute(sa.text("select trigger from jobs")).scalar() == "manual"
        assert [str(o).replace("-", "") for o in c.execute(sa.text("select owner_id from pipelines")).scalars()] == [str(admin).replace("-", "")]
    with eng.begin() as c, pytest.raises(sa.exc.IntegrityError):
        c.execute(sa.text("update users set role = 'member'"))
    # deleting the user keeps their work, unowned (the FK action itself; the API also clears owner_id)
    with eng.connect() as c:
        if eng.dialect.name == "sqlite":
            c.exec_driver_sql("PRAGMA foreign_keys=ON")
        c.execute(sa.text("delete from users"))
        c.commit()
        for t in ("connections", "jobs", "pipelines"):
            assert c.execute(sa.text(f"select count(*), count(owner_id) from {t}")).one() == (1, 0), t


def test_migration_turns_a_single_admin_install_into_one_superadmin(env, tmp_path):
    url = f"sqlite:///{tmp_path / 'app.db'}"
    eng, cfg, admin = _single_admin_install(url, from_base=False)
    _check_upgraded(eng, admin)
    command.downgrade(cfg, "0008")  # and back
    insp = sa.inspect(eng)
    assert "token_version" not in [c["name"] for c in insp.get_columns("users")]
    eng.dispose()


def test_migration_keeps_an_install_with_an_active_superadmin(env, tmp_path):
    """Its only admin deactivated: the oldest active user becomes the superadmin, so nobody is locked out."""
    url = f"sqlite:///{tmp_path / 'app.db'}"
    cfg = _alembic(url)
    command.stamp(cfg, "head")
    command.downgrade(cfg, "0008")
    eng = sa.create_engine(url)
    old = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    with eng.begin() as c:
        _insert(c, "users", id=_id(eng), username="gone", role="admin", is_active=False, created_at=old)
        _insert(c, "users", id=_id(eng), username="ann", role="member", is_active=True, created_at=old + dt.timedelta(days=1))
        _insert(c, "users", id=_id(eng), username="ben", role="viewer", is_active=True, created_at=old + dt.timedelta(days=2))
    command.upgrade(cfg, "head")
    with eng.connect() as c:
        roles = dict(c.execute(sa.text("select username, role from users")).all())
    assert roles == {"gone": "superadmin", "ann": "superadmin", "ben": "viewer"}
    eng.dispose()


@needs_pg
def test_migration_on_postgres_from_an_empty_database(env):
    import psycopg

    db = uniq("mig")
    admin_url = f"host={PG['host']} port={PG['port']} dbname={PG['database']} user={PG['user']} password=conduit"
    with psycopg.connect(admin_url, autocommit=True) as c:
        c.execute(f'create database "{db}"')
    url = f"postgresql+psycopg://{PG['user']}:conduit@{PG['host']}:{PG['port']}/{db}"
    try:
        eng, cfg, admin = _single_admin_install(url, from_base=True)
        fks = {t: [f["options"].get("ondelete") for f in sa.inspect(eng).get_foreign_keys(t) if f["constrained_columns"] == ["owner_id"]]
               for t in ("connections", "jobs", "service_checks", "pipelines", "comparisons")}
        assert fks == {t: ["SET NULL"] for t in fks}
        _check_upgraded(eng, admin)
        command.downgrade(cfg, "0008")
        command.upgrade(cfg, "head")
        eng.dispose()
    finally:
        with psycopg.connect(admin_url, autocommit=True) as c:
            c.execute(f'drop database if exists "{db}" with (force)')
