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
