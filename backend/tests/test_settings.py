"""Settings (Control & Setup): classes, sources, bounds, reset, and a change reaching another process in time."""
import pytest

from app import runtime
from app.config import Settings, get_settings


def _get(client, auth, key):
    return next(s for s in client.get("/api/settings", headers=auth).json() if s["key"] == key)


def test_every_setting_declares_a_class():
    assert set(runtime.SPECS) == set(Settings.model_fields), "a new setting needs a Spec (runtime / restart / hidden)"
    for s in runtime.SPECS.values():
        assert s.cls in ("runtime", "restart", "hidden")
        if s.cls != "hidden":
            assert s.group in runtime.GROUPS and s.label and s.help, s.key
        if s.cls == "runtime" and runtime.kind(s.key) is not str:
            assert s.lo is not None and (s.hi is not None or s.key == "upload_max_mb"), s.key
            assert s.lo <= runtime.default(s.key) <= (s.hi or 10 ** 12), s.key  # the default is inside its own bounds


def test_the_list_shows_runtime_and_restart_settings_never_hidden_ones(client, auth):
    items = client.get("/api/settings", headers=auth).json()
    keys = {s["key"] for s in items}
    for hidden in ("secret_key", "jwt_secret", "database_url", "redis_url", "admin_password", "telegram_api_url"):
        assert hidden not in keys
    assert {"keep_finished_jobs", "upload_max_mb", "public_url", "monitor_warn_pct"} <= keys
    assert {"health_check_interval_s", "upload_ceiling_mb", "jwt_ttl_minutes"} <= keys
    groups = [s["group"] for s in items]
    assert groups == sorted(groups, key=list(runtime.GROUPS).index)  # grouped, in the page's order
    k = _get(client, auth, "keep_finished_jobs")
    assert (k["cls"], k["type"], k["value"], k["default"], k["source"], k["min"], k["max"]) == ("runtime", "int", 10, 10, "default", 1, 1000)
    assert k["env"] == "CONDUIT_KEEP_FINISHED_JOBS" and k["help"]
    assert _get(client, auth, "connect_timeout_s")["source"] == "env"  # conftest sets CONDUIT_CONNECT_TIMEOUT_S=3
    assert _get(client, auth, "health_check_interval_s")["cls"] == "restart"


def test_override_then_reset_with_dotenv_underneath(client, auth, monkeypatch):
    monkeypatch.setenv("CONDUIT_KEEP_FINISHED_JOBS", "7")
    get_settings.cache_clear()
    k = _get(client, auth, "keep_finished_jobs")
    assert (k["value"], k["source"], k["env_value"]) == (7, "env", 7)

    r = client.put("/api/settings/keep_finished_jobs", json={"value": 3}, headers=auth)
    assert r.status_code == 200, r.text
    k = r.json()
    assert (k["value"], k["source"], k["env_value"], k["updated_by"]) == (3, "ui", 7, "alice") and k["updated_at"]
    assert runtime.current_settings().keep_finished_jobs == 3 and get_settings().keep_finished_jobs == 7  # never mutated

    k = client.delete("/api/settings/keep_finished_jobs", headers=auth).json()
    assert (k["value"], k["source"], k["updated_by"]) == (7, "env", None)
    assert runtime.current_settings().keep_finished_jobs == 7
    assert client.delete("/api/settings/keep_finished_jobs", headers=auth).status_code == 200  # nothing to reset: fine


@pytest.mark.parametrize("key,value,why", [
    ("keep_finished_jobs", 0, "between 1 and 1,000"),
    ("keep_finished_jobs", 5000, "between 1 and 1,000"),
    ("keep_finished_jobs", 2.5, "a whole number"),
    ("keep_finished_jobs", "lots", "expected a number"),
    ("keep_finished_jobs", True, "expected a number"),
    ("keep_finished_jobs", None, "expected a number"),
    ("monitor_warn_pct", 100, "between 50 and 99"),
    ("combined_min_free_pct", 95, "between 0 and 90"),
    ("upload_max_mb", 4096, "between 1 and 2,048 MB"),  # never above the ceiling nginx was started with
    ("public_url", "conduit.example.com", "http(s) address"),
    ("public_url", "https://conduit.example.com/?x=1", "http(s) address"),
    ("public_url", 5, "expected text"),
    ("instance_name", "  ", "1 to 60 characters"),
    ("instance_name", "x" * 61, "1 to 60 characters"),
])
def test_bounds_and_types_are_checked(client, auth, key, value, why):
    r = client.put(f"/api/settings/{key}", json={"value": value}, headers=auth)
    assert r.status_code == 422 and why in r.json()["detail"], r.text
    assert _get(client, auth, key)["source"] != "ui"


def test_values_are_normalised(client, auth):
    assert client.put("/api/settings/public_url", json={"value": " https://conduit.example.com/ "}, headers=auth).json()["value"] == "https://conduit.example.com"
    assert client.put("/api/settings/public_url", json={"value": ""}, headers=auth).json()["value"] == ""
    assert client.put("/api/settings/remote_max_files", json={"value": "1,200"}, headers=auth).json()["value"] == 1200
    assert client.put("/api/settings/combined_min_free_gb", json={"value": 2.5}, headers=auth).json()["value"] == 2.5
    assert client.put("/api/settings/instance_name", json={"value": " Prod EU "}, headers=auth).json()["value"] == "Prod EU"


def test_restart_required_hidden_and_unknown_keys_are_refused(client, auth):
    r = client.put("/api/settings/health_check_interval_s", json={"value": 60}, headers=auth)
    assert r.status_code == 422 and "CONDUIT_HEALTH_CHECK_INTERVAL_S" in r.json()["detail"] and "restart" in r.json()["detail"]
    assert client.put("/api/settings/upload_ceiling_mb", json={"value": 9999}, headers=auth).status_code == 422
    for key in ("secret_key", "database_url", "jwt_secret", "redis_url", "alerts.mute_until", "nope"):
        assert client.put(f"/api/settings/{key}", json={"value": "x"}, headers=auth).status_code == 404, key
        assert client.delete(f"/api/settings/{key}", headers=auth).status_code == 404, key


def test_another_process_sees_a_change_within_the_cache_window(client, auth, monkeypatch):
    """The worker never hears about a save: it re-reads the overrides at most CACHE_S later."""
    from app.alerts import engine
    from app.db import get_sessionmaker
    from app.models import Setting
    from app.monitor import host

    clock = [1000.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    assert host._level(90) == "high" and runtime.current_settings().monitor_warn_pct == 85  # cached now: no override

    with get_sessionmaker()() as s:  # what the API process does, as far as this "worker" can tell
        s.add(Setting(key="monitor_warn_pct", value=95))
        s.commit()
    clock[0] += runtime.CACHE_S - 1
    assert host._level(90) == "high"  # still inside the window: the old value
    clock[0] += 2
    assert host._level(90) == "ok" and host._level(96) == "high"  # the change arrived without a restart
    assert host.snapshot()["warn_at_pct"] == 95
    assert runtime.current_settings().monitor_warn_pct == 95
    assert engine.current_settings().monitor_warn_pct == 95  # the host alert's default threshold follows


def test_a_saved_value_that_no_longer_fits_is_ignored(client, auth, monkeypatch):
    assert client.put("/api/settings/upload_max_mb", json={"value": 1500}, headers=auth).status_code == 200
    monkeypatch.setenv("CONDUIT_UPLOAD_CEILING_MB", "1000")  # .env lowered the ceiling, then a restart
    get_settings.cache_clear()
    runtime.invalidate()
    u = _get(client, auth, "upload_max_mb")
    assert u["value"] == 1000 and "ignored" in u["note"] and u["max"] == 1000
    assert runtime.current_settings().upload_max_mb == 1000


def test_the_upload_limit_applies_at_once(client, auth):
    assert client.put("/api/settings/upload_max_mb", json={"value": 1}, headers=auth).status_code == 200
    r = client.post("/api/files/upload", files={"file": ("big.csv", b"x" * (1024 * 1024 + 10))}, headers=auth)
    assert r.status_code == 413 and "larger than 1 MB" in r.json()["detail"]
    assert client.post("/api/files/upload", files={"file": ("ok.csv", b"a\n1\n")}, headers=auth).status_code == 201
    client.delete("/api/settings/upload_max_mb", headers=auth)
    assert client.post("/api/files/upload", files={"file": ("big.csv", b"x" * (1024 * 1024 + 10))}, headers=auth).status_code == 201


def test_a_slow_check_threshold_applies(client, auth):
    from app.monitor import checks

    assert client.put("/api/settings/connection_slow_ms", json={"value": 50}, headers=auth).status_code == 200
    from app.connectors.base import _slow_ms
    assert _slow_ms() == 50
    assert client.put("/api/settings/check_slow_ms", json={"value": 75}, headers=auth).status_code == 200
    assert checks.current_settings().check_slow_ms == 75


def test_settings_are_admin_only(client, auth_viewer):
    assert client.get("/api/settings", headers=auth_viewer).status_code == 403
    assert client.put("/api/settings/keep_finished_jobs", json={"value": 3}, headers=auth_viewer).status_code == 403
