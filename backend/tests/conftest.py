import os

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

# Settings must be in place before app modules read them.
os.environ.setdefault("CONDUIT_SECRET_KEY", Fernet.generate_key().decode())
os.environ["CONDUIT_ADMIN_PASSWORD"] = "test-pass-123"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Fresh app database (SQLite file), fresh data dir, cleared settings/engine caches."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("CONDUIT_DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("CONDUIT_DATA_DIR", str(data))
    monkeypatch.setenv("CONDUIT_ALLOWED_FILE_ROOTS", "")
    monkeypatch.setenv("CONDUIT_CONNECT_TIMEOUT_S", "3")
    monkeypatch.setenv("CONDUIT_JOB_RUNNER", "thread")  # run jobs inside the test process, no Celery needed
    monkeypatch.setenv("CONDUIT_REDIS_URL", "memory://")  # job log stays in memory

    from app.config import get_settings
    from app.db import Base, get_engine, reset_engine_cache
    from app import models  # noqa: F401

    get_settings.cache_clear()
    reset_engine_cache()
    Base.metadata.create_all(get_engine())
    yield data
    get_engine().dispose()
    get_settings.cache_clear()
    reset_engine_cache()


@pytest.fixture()
def client(env):
    from app.main import create_app

    with TestClient(create_app()) as c:  # lifespan seeds the admin
        yield c


@pytest.fixture()
def auth(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "test-pass-123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
