def test_login_ok_and_me(client, auth_superadmin):
    r = client.get("/api/auth/me", headers=auth_superadmin)
    assert r.status_code == 200
    assert r.json()["username"] == "admin" and r.json()["role"] == "superadmin"  # the first-run account
    assert "password_hash" not in r.text and "scrypt" not in r.text


def test_wrong_password_and_unknown_user_look_the_same(client):
    a = client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    b = client.post("/api/auth/login", json={"username": "ghost", "password": "nope"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_protected_routes_need_a_token(client):
    for path in ("/api/connections", "/api/meta/db-types", "/api/auth/me"):
        assert client.get(path).status_code == 401
    assert client.get("/api/connections", headers={"Authorization": "Bearer junk"}).status_code == 401


def test_health_is_public(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_login_is_throttled_after_repeated_failures(client):
    from app.api import auth as auth_mod

    auth_mod._FAILS.clear()
    codes = [client.post("/api/auth/login", json={"username": "admin", "password": "bad"}).status_code
             for _ in range(12)]
    assert codes[:10] == [401] * 10
    assert codes[10] == 429
    auth_mod._FAILS.clear()


def test_one_users_failed_logins_dont_block_another(client):
    from app.api import auth as auth_mod
    from conftest import make_user

    auth_mod._FAILS.clear()
    make_user("bob", "admin")
    for _ in range(10):
        assert client.post("/api/auth/login", json={"username": "admin", "password": "bad"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": "test-pass-123"}).status_code == 429
    assert client.post("/api/auth/login", json={"username": "bob", "password": "test-pass-123"}).status_code == 200
    assert client.post("/api/auth/login", json={"username": "BOB", "password": "bad"}).status_code == 401  # not 429
    auth_mod._FAILS.clear()


def test_behind_nginx_failures_count_per_real_client_and_the_header_is_trusted_from_nginx_only(client):
    """The same uvicorn middleware docker-compose.yml turns on (--proxy-headers --forwarded-allow-ips <nginx>)."""
    from starlette.testclient import TestClient
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    from app.api import auth as auth_mod

    auth_mod._FAILS.clear()
    app = ProxyHeadersMiddleware(client.app, trusted_hosts="10.9.9.9")
    nginx, direct = TestClient(app, client=("10.9.9.9", 5000)), TestClient(app, client=("192.0.2.66", 5000))
    bad, good = {"username": "admin", "password": "bad"}, {"username": "admin", "password": "test-pass-123"}

    for _ in range(10):
        assert nginx.post("/api/auth/login", json=bad, headers={"X-Forwarded-For": "203.0.113.5"}).status_code == 401
    assert nginx.post("/api/auth/login", json=good, headers={"X-Forwarded-For": "203.0.113.5"}).status_code == 429
    # someone else behind the same nginx, same username: still fine
    assert nginx.post("/api/auth/login", json=good, headers={"X-Forwarded-For": "198.51.100.7"}).status_code == 200

    # not from nginx: the header is ignored, every attempt counts for the real peer whatever address it claims
    for i in range(10):
        assert direct.post("/api/auth/login", json=bad, headers={"X-Forwarded-For": f"198.51.100.{i}"}).status_code == 401
    assert direct.post("/api/auth/login", json=good, headers={"X-Forwarded-For": "198.51.100.200"}).status_code == 429
    auth_mod._FAILS.clear()


def test_the_api_access_log_never_shows_a_token():
    import logging

    from app.main import HideTokens

    args = ("172.30.80.10:41234", "GET", "/api/jobs/1/events?token=eyJ.abc.def&x=1", "1.1", 200)
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', args, None)
    assert HideTokens().filter(rec)
    assert rec.getMessage() == '172.30.80.10:41234 - "GET /api/jobs/1/events?token=***&x=1 HTTP/1.1" 200'
    assert any(isinstance(f, HideTokens) for f in logging.getLogger("uvicorn.access").filters)
