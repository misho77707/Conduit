"""Every /api route declares exactly one permission (app/deps.py), and it is the one this table says.

A new route fails here until it declares a permission and is added to EXPECTED: there is no silent allow.
"""
import re
import uuid

from fastapi.routing import APIRoute

from conftest import make_user, login

PUBLIC = {("POST", "/api/auth/login"), ("GET", "/api/health")}

SELF, VIEWER, VIEWER_SSE, ADMIN, SUPER = "self_service", "viewer_ok", "viewer_ok_sse", "admin_only", "superadmin_only"

EXPECTED = {
    ("GET", "/api/auth/me"): SELF,
    ("POST", "/api/auth/password"): SELF,
    # user management
    ("GET", "/api/users"): SUPER,
    ("POST", "/api/users"): SUPER,
    ("PATCH", "/api/users/{user_id}"): SUPER,
    ("POST", "/api/users/{user_id}/reset-password"): SUPER,
    ("DELETE", "/api/users/{user_id}"): SUPER,
    # connections and the add-connection flow
    ("GET", "/api/connections"): ADMIN,
    ("POST", "/api/connections"): ADMIN,
    ("POST", "/api/connections/test"): ADMIN,
    ("POST", "/api/connections/test-all"): ADMIN,
    ("GET", "/api/connections/{conn_id}"): ADMIN,
    ("PATCH", "/api/connections/{conn_id}"): ADMIN,
    ("DELETE", "/api/connections/{conn_id}"): ADMIN,
    ("POST", "/api/connections/{conn_id}/test"): ADMIN,
    ("GET", "/api/connections/{conn_id}/tables"): ADMIN,
    ("GET", "/api/connections/{conn_id}/tables/{table}/inspect"): ADMIN,
    ("GET", "/api/meta/db-types"): ADMIN,
    # files and remote files
    ("POST", "/api/files/inspect"): ADMIN,
    ("GET", "/api/files/roots"): ADMIN,
    ("GET", "/api/files/list"): ADMIN,
    ("POST", "/api/files/upload"): ADMIN,
    ("GET", "/api/remote/{connection_id}/list"): ADMIN,
    ("POST", "/api/remote/match"): ADMIN,
    ("POST", "/api/remote/inspect"): ADMIN,
    # jobs
    ("POST", "/api/jobs/plan"): ADMIN,
    ("POST", "/api/jobs"): ADMIN,
    ("GET", "/api/jobs"): VIEWER,
    ("GET", "/api/jobs/{job_id}"): VIEWER,
    ("GET", "/api/jobs/{job_id}/log"): VIEWER,
    ("GET", "/api/jobs/{job_id}/events"): VIEWER_SSE,
    ("POST", "/api/jobs/{job_id}/cancel"): ADMIN,
    ("POST", "/api/jobs/{job_id}/retry"): ADMIN,
    ("DELETE", "/api/jobs/{job_id}"): ADMIN,
    ("GET", "/api/jobs/{job_id}/rejects"): ADMIN,
    # monitor
    ("GET", "/api/monitor"): VIEWER,
    ("POST", "/api/monitor/refresh"): ADMIN,
    ("GET", "/api/monitor/checks"): ADMIN,
    ("POST", "/api/monitor/checks"): ADMIN,
    ("PATCH", "/api/monitor/checks/{check_id}"): ADMIN,
    ("DELETE", "/api/monitor/checks/{check_id}"): ADMIN,
    ("POST", "/api/monitor/checks/{check_id}/run"): ADMIN,
    # pipelines
    ("GET", "/api/pipelines"): VIEWER,
    ("POST", "/api/pipelines"): ADMIN,
    ("GET", "/api/pipelines/{pipeline_id}"): ADMIN,
    ("PATCH", "/api/pipelines/{pipeline_id}"): ADMIN,
    ("DELETE", "/api/pipelines/{pipeline_id}"): ADMIN,
    ("POST", "/api/pipelines/{pipeline_id}/preview"): ADMIN,
    ("POST", "/api/pipelines/{pipeline_id}/run"): ADMIN,
    # compare
    ("GET", "/api/compare"): VIEWER,
    ("GET", "/api/compare/{cid}"): VIEWER,
    ("POST", "/api/compare/columns"): ADMIN,
    ("POST", "/api/compare"): ADMIN,
    ("GET", "/api/compare/{cid}/rows"): ADMIN,
    ("GET", "/api/compare/{cid}/export"): ADMIN,
    ("POST", "/api/compare/{cid}/cancel"): ADMIN,
    ("DELETE", "/api/compare/{cid}"): ADMIN,
}


def _flat(routes):
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif hasattr(r, "effective_route_contexts"):  # an included router (FastAPI keeps them as one route)
            yield from r.effective_route_contexts()
        elif hasattr(r, "routes"):
            yield from _flat(r.routes)


def _permissions(dependant) -> set[str]:
    found = set()
    for d in dependant.dependencies:
        if getattr(d.call, "permission", None):
            found.add(d.call.permission)
        found |= _permissions(d)
    return found


def api_routes(app) -> dict[tuple[str, str], set[str]]:
    out = {}
    for r in _flat(app.routes):
        if r.path.startswith("/api"):
            for m in r.methods - {"HEAD", "OPTIONS"}:
                out[(m, r.path)] = _permissions(r.dependant)
    return out


def _url(path: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: "t" if m.group(1) == "table" else str(uuid.uuid4()), path)


def test_every_api_route_declares_exactly_one_permission(client):
    routes = api_routes(client.app)
    missing = sorted(k for k, p in routes.items() if k not in PUBLIC and len(p) != 1)
    assert not missing, f"routes without exactly one permission: {missing}"
    assert all(not routes[k] for k in PUBLIC if k in routes), "login / health take no permission"


def test_permissions_match_the_table(client):
    routes = {k: next(iter(p)) for k, p in api_routes(client.app).items() if k not in PUBLIC}
    assert routes == EXPECTED, {"not in the table": sorted(set(routes) - set(EXPECTED)),
                                "in the table but gone": sorted(set(EXPECTED) - set(routes)),
                                "different": sorted(k for k in routes if k in EXPECTED and routes[k] != EXPECTED[k])}


def test_a_viewer_gets_403_from_every_route_above_viewer(client, auth_viewer):
    wrong = []
    for (method, path), perm in EXPECTED.items():
        if perm not in (ADMIN, SUPER):
            continue
        r = client.request(method, _url(path), json={}, headers=auth_viewer)
        if r.status_code != 403 or r.json()["detail"]["code"] != "forbidden":
            wrong.append((method, path, r.status_code, r.text[:120]))
    assert not wrong, wrong


def test_an_admin_gets_403_from_superadmin_routes(client, auth_admin):
    wrong = []
    for (method, path), perm in EXPECTED.items():
        if perm == SUPER:
            r = client.request(method, _url(path), json={}, headers=auth_admin)
            if r.status_code != 403:
                wrong.append((method, path, r.status_code))
    assert not wrong, wrong


def test_a_viewer_is_let_into_viewer_routes(client, auth_viewer):
    for (method, path), perm in EXPECTED.items():
        if perm in (VIEWER, SELF) and method == "GET":
            r = client.request(method, _url(path), headers=auth_viewer)
            assert r.status_code in (200, 404), (method, path, r.status_code, r.text[:200])  # 404: the random id


def test_authorization_uses_the_stored_role_not_the_token(client):
    from app.db import get_sessionmaker
    from app.models import User

    uid = make_user("bob", "admin")
    h = login(client, "bob")  # the token says "admin"
    assert client.get("/api/connections", headers=h).status_code == 200
    with get_sessionmaker()() as s:
        s.get(User, uid).role = "viewer"
        s.commit()
    assert client.get("/api/connections", headers=h).status_code == 403
    assert client.get("/api/jobs", headers=h).status_code == 200


def test_a_pending_password_change_blocks_everything_but_me(client):
    make_user("newbie", "admin", must_change_password=True)
    h = login(client, "newbie")
    r = client.get("/api/jobs", headers=h)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "password_change_required", r.text
    me = client.get("/api/auth/me", headers=h)
    assert me.status_code == 200 and me.json()["username"] == "newbie"


def test_no_token_or_a_bad_one_is_401(client):
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/jobs", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get(f"/api/jobs/{uuid.uuid4()}/events?token=nope").status_code == 401
