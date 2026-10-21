"""Users API (superadmins), passwords, sessions, and whom scheduled runs run as."""
import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from conftest import PASSWORD, login, make_user
from jobhelpers import wait_job
from test_pipelines_api import _pipeline_body, _seed, _sqlite_conn

TEMP = "temporary-pass-1"


def _create(client, h, username, role="viewer", password=TEMP):
    return client.post("/api/users", json={"username": username, "role": role, "temporary_password": password}, headers=h)


def _set_password(client, h, current, new):
    return client.post("/api/auth/password", json={"current_password": current, "new_password": new}, headers=h)


def _bearer(r) -> dict:
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _me(client, h) -> dict:
    return client.get("/api/auth/me", headers=h).json()


def test_a_superadmin_adds_lists_changes_and_deletes_users(client, auth_superadmin):
    r = _create(client, auth_superadmin, "viv")
    assert r.status_code == 201, r.text
    u = r.json()
    assert (u["role"], u["must_change_password"], u["is_active"], u["created_by"], u["last_login_at"]) == ("viewer", True, True, "admin", None)
    login(client, "viv", TEMP)
    rows = {x["username"]: x for x in client.get("/api/users", headers=auth_superadmin).json()}
    assert rows["viv"]["last_login_at"] and rows["admin"]["created_by"] is None and rows["admin"]["role"] == "superadmin"

    r = client.patch(f"/api/users/{u['id']}", json={"role": "admin"}, headers=auth_superadmin)
    assert r.status_code == 200 and r.json()["role"] == "admin"
    assert client.patch(f"/api/users/{u['id']}", json={"is_active": False}, headers=auth_superadmin).json()["is_active"] is False
    assert client.patch(f"/api/users/{u['id']}", json={"is_active": True}, headers=auth_superadmin).json()["is_active"] is True
    assert client.delete(f"/api/users/{u['id']}", headers=auth_superadmin).status_code == 204
    assert "viv" not in [x["username"] for x in client.get("/api/users", headers=auth_superadmin).json()]
    assert client.delete(f"/api/users/{u['id']}", headers=auth_superadmin).status_code == 404


def test_usernames_are_unique_ignoring_case(client, auth_superadmin):
    assert _create(client, auth_superadmin, "Alex").status_code == 201
    for name in ("alex", "ADMIN"):
        r = _create(client, auth_superadmin, name)
        assert r.status_code == 409 and "already a user" in r.json()["detail"], r.text


@pytest.mark.parametrize("body, says", [
    ({"username": "sam", "role": "viewer", "temporary_password": "short"}, "at least 10"),
    ({"username": "samuel.long", "role": "viewer", "temporary_password": "Samuel.Long"}, "can't be the username"),
    ({"username": "has space", "role": "viewer", "temporary_password": TEMP}, "letters, digits"),
    ({"username": "sam", "role": "owner", "temporary_password": TEMP}, ""),
])
def test_new_users_are_checked(client, auth_superadmin, body, says):
    r = client.post("/api/users", json=body, headers=auth_superadmin)
    assert r.status_code == 422 and says in r.text, r.text


@pytest.mark.parametrize("change", ["delete", "demote", "deactivate"])
def test_the_last_superadmin_cant_go_not_even_by_themselves(client, auth_superadmin, change):
    me = _me(client, auth_superadmin)["id"]
    r = {"delete": lambda: client.delete(f"/api/users/{me}", headers=auth_superadmin),
         "demote": lambda: client.patch(f"/api/users/{me}", json={"role": "admin"}, headers=auth_superadmin),
         "deactivate": lambda: client.patch(f"/api/users/{me}", json={"is_active": False}, headers=auth_superadmin)}[change]()
    assert r.status_code == 409 and "last active superadmin" in r.json()["detail"], r.text
    assert _me(client, auth_superadmin)["role"] == "superadmin"


def test_with_two_superadmins_one_may_go_but_not_the_last(client, auth_superadmin):
    other = _create(client, auth_superadmin, "root2", "superadmin").json()["id"]
    h2 = _bearer(_set_password(client, login(client, "root2", TEMP), TEMP, "root2-own-pass"))
    me = _me(client, auth_superadmin)["id"]
    # an inactive superadmin doesn't count
    assert client.patch(f"/api/users/{other}", json={"is_active": False}, headers=auth_superadmin).status_code == 200
    assert client.patch(f"/api/users/{me}", json={"role": "admin"}, headers=auth_superadmin).status_code == 409
    assert client.patch(f"/api/users/{other}", json={"is_active": True}, headers=auth_superadmin).status_code == 200
    h2 = login(client, "root2", "root2-own-pass")  # deactivating ended its session
    # now the first one may demote itself (its session ends); root2 is the last and can't leave
    assert client.patch(f"/api/users/{me}", json={"role": "admin"}, headers=auth_superadmin).status_code == 200
    assert client.get("/api/auth/me", headers=auth_superadmin).status_code == 401
    for r in (client.patch(f"/api/users/{other}", json={"role": "viewer"}, headers=h2),
              client.patch(f"/api/users/{other}", json={"is_active": False}, headers=h2),
              client.delete(f"/api/users/{other}", headers=h2)):
        assert r.status_code == 409, r.text
    assert client.delete(f"/api/users/{me}", headers=h2).status_code == 204  # a non-superadmin goes fine


def test_a_temporary_password_must_be_replaced_first(client, auth_superadmin):
    _create(client, auth_superadmin, "newbie.user", "admin")
    h = login(client, "newbie.user", TEMP)
    assert _me(client, h)["must_change_password"] is True
    r = client.get("/api/connections", headers=h)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "password_change_required"
    assert _set_password(client, h, "not-the-password", "a-better-pass-9").status_code == 400
    for bad, says in (("short", "at least 10"), ("Newbie.User", "can't be the username"), (TEMP, "different from the current")):
        r = _set_password(client, h, TEMP, bad)
        assert r.status_code == 422 and says in r.json()["detail"], r.text
    new = _bearer(_set_password(client, h, TEMP, "a-better-pass-9"))
    assert _me(client, new)["must_change_password"] is False
    assert client.get("/api/connections", headers=new).status_code == 200
    assert client.get("/api/auth/me", headers=h).status_code == 401  # the session that changed it moves to the new token
    login(client, "newbie.user", "a-better-pass-9")


def test_every_role_changes_their_own_password(client, auth_viewer):
    other_session = login(client, "victor")
    _bearer(_set_password(client, auth_viewer, PASSWORD, "viewer-pass-22"))
    assert client.get("/api/jobs", headers=other_session).status_code == 401  # other sessions end
    login(client, "victor", "viewer-pass-22")


def test_a_password_reset_ends_the_users_sessions(client, auth_superadmin):
    uid = make_user("vic", "viewer")
    h = login(client, "vic")
    assert client.get("/api/jobs", headers=h).status_code == 200
    r = client.post(f"/api/users/{uid}/reset-password", json={"temporary_password": TEMP}, headers=auth_superadmin)
    assert r.status_code == 200 and r.json()["must_change_password"] is True
    assert client.get("/api/jobs", headers=h).status_code == 401
    assert client.get("/api/jobs", headers=login(client, "vic", TEMP)).json()["detail"]["code"] == "password_change_required"
    me = _me(client, auth_superadmin)["id"]
    r = client.post(f"/api/users/{me}/reset-password", json={"temporary_password": TEMP}, headers=auth_superadmin)
    assert r.status_code == 422 and "Change password" in r.json()["detail"]


def test_a_role_change_ends_the_users_sessions(client, auth_superadmin):
    uid = make_user("ada", "admin")
    h = login(client, "ada")
    assert client.patch(f"/api/users/{uid}", json={"role": "viewer"}, headers=auth_superadmin).status_code == 200
    assert client.get("/api/jobs", headers=h).status_code == 401
    h = login(client, "ada")
    assert client.get("/api/connections", headers=h).status_code == 403
    assert client.get("/api/jobs", headers=h).status_code == 200


def test_a_deactivated_user_cant_sign_in_and_their_token_stops_working(client, auth_superadmin):
    uid = make_user("dee", "admin")
    h = login(client, "dee")
    assert client.patch(f"/api/users/{uid}", json={"is_active": False}, headers=auth_superadmin).status_code == 200
    assert client.get("/api/jobs", headers=h).status_code == 401
    assert client.get(f"/api/jobs/{uuid.uuid4()}/events?token={h['Authorization'][7:]}").status_code == 401
    right = client.post("/api/auth/login", json={"username": "dee", "password": PASSWORD})
    wrong = client.post("/api/auth/login", json={"username": "dee", "password": "wrong-password"})
    assert right.status_code == wrong.status_code == 401 and right.json() == wrong.json()
    client.patch(f"/api/users/{uid}", json={"is_active": True}, headers=auth_superadmin)
    login(client, "dee")


def test_deleting_a_user_keeps_their_work(client, auth_superadmin):
    from app.db import get_sessionmaker
    from app.models import Connection, Job, Pipeline, User

    uid = make_user("leaver", "admin")
    with get_sessionmaker()() as s:
        s.add_all([Connection(name="theirs", type="sqlite", config={}, owner_id=uid),
                   Pipeline(name="theirs", source={}, target={}, options={}, steps=[], owner_id=uid),
                   Job(name="theirs", owner_id=uid),
                   User(username="protege", role="viewer", password_hash="x", created_by_id=uid)])
        s.commit()
    assert client.delete(f"/api/users/{uid}", headers=auth_superadmin).status_code == 204
    with get_sessionmaker()() as s:
        for model in (Connection, Pipeline, Job):
            rows = s.scalars(select(model).where(model.name == "theirs")).all()
            assert len(rows) == 1 and rows[0].owner_id is None, model
        assert s.scalar(select(User).where(User.username == "protege")).created_by_id is None


def test_a_generated_first_run_password_must_be_changed(env, monkeypatch):
    from app.bootstrap import seed_admin
    from app.config import get_settings
    from app.db import get_sessionmaker
    from app.models import User

    monkeypatch.setenv("CONDUIT_ADMIN_PASSWORD", "")
    get_settings.cache_clear()
    seed_admin()
    with get_sessionmaker()() as s:
        u = s.scalar(select(User))
        assert (u.username, u.role, u.must_change_password) == ("admin", "superadmin", True)


# ---- scheduled runs ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("case", ["owner", "owner deactivated", "oldest is a viewer"])
def test_scheduled_runs_run_as_an_active_admin(client, auth_superadmin, auth_admin, env, case):
    """Never as a viewer or a deactivated account; the job is marked as started by the schedule."""
    from app.db import get_sessionmaker
    from app.engine.schedule import run_due
    from app.models import Job, Pipeline, User, utcnow

    src = _sqlite_conn(client, auth_admin, _seed(env))
    dst = _sqlite_conn(client, auth_admin, env / "dst.sqlite")
    pid = client.post("/api/pipelines", json=_pipeline_body(src, dst, schedule="@daily"), headers=auth_admin).json()["id"]
    assert client.post(f"/api/pipelines/{pid}/preview", json={"sample_rows": 5}, headers=auth_admin).status_code == 200
    with get_sessionmaker()() as s:
        alice = s.scalar(select(User).where(User.username == "alice"))
        p = s.get(Pipeline, uuid.UUID(pid))
        assert p.owner_id == alice.id  # pipelines record who created them
        p.next_run_at = utcnow() - dt.timedelta(minutes=1)
        if case == "owner deactivated":
            alice.is_active = False
        if case == "oldest is a viewer":
            p.owner_id = None  # e.g. a pipeline from before owners were recorded
            s.add(User(username="elder", role="viewer", password_hash="x", created_at=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)))
        s.commit()

    assert run_due() == 1
    with get_sessionmaker()() as s:
        job = s.get(Job, s.get(Pipeline, uuid.UUID(pid)).last_job_id)
        runner = s.get(User, job.owner_id)
        assert job.trigger == "schedule"
        assert runner.username == ("alice" if case == "owner" else "admin")  # else: the superadmin, never the viewer
    assert wait_job(client, auth_superadmin, str(job.id))["status"] == "succeeded"
