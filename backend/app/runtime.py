"""Runtime settings: config values admins change on Control & Setup > Settings, without a restart.

Every field of config.Settings declares a class here (tests/test_settings.py fails otherwise):
  runtime  editable on the Settings page. The override is a row in the `settings` table; every process (API,
           worker, beat) picks it up within CACHE_S, the process that saved it at once.
  restart  shown read-only: set it in .env and restart (read once at start, a mount, or security policy).
  hidden   never shown or exported (secrets, connection strings, first-start values, test hooks).
Effective value: the UI override, else .env (the environment), else the default. get_settings() stays the
environment's view and is never changed: code reading a runtime value calls current_settings() instead.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import get_sessionmaker
from .models import Setting, User

log = logging.getLogger("conduit.settings")

CACHE_S = 15.0


@dataclass(frozen=True)
class Spec:
    key: str
    cls: str  # runtime | restart | hidden
    group: str = ""
    label: str = ""
    help: str = ""
    lo: float | None = None
    hi: float | None = None
    unit: str = ""


GROUPS = {"jobs": "Jobs & retention", "files": "Uploads & remote files", "combined": "Combined sources",
          "monitor": "Monitor", "alerts": "Alerts", "system": "System"}

_ALL = (
    Spec("keep_finished_jobs", "runtime", "jobs", "Finished jobs kept",
         "Only the latest finished jobs are kept (no history): older ones and their reject files are deleted.", 1, 1000, "jobs"),
    Spec("keep_comparisons", "runtime", "jobs", "Comparisons kept",
         "Only the latest finished comparisons and their result files are kept.", 1, 100, "comparisons"),
    Spec("max_workers_per_job", "runtime", "jobs", "Workers per job, at most",
         "Caps the parallel splits one job may ask for. Applies to jobs that start afterwards.", 1, 128, "threads"),
    Spec("connect_timeout_s", "runtime", "jobs", "Connection timeout",
         "Opening a database connection (test, health check, job) gives up after this long.", 1, 120, "s"),
    Spec("jwt_ttl_minutes", "restart", "system", "Session length",
         "A sign-in lasts this long. Security policy, so it is set in .env only.", unit="min"),
    Spec("data_dir", "restart", "system", "Data folder", "Uploads, rejects, caches and file databases live here."),
    Spec("allowed_file_roots", "restart", "system", "Allowed file roots",
         "File paths must resolve inside these folders (defaults to the data folder). Depends on mounts."),
    Spec("cors_origins", "restart", "system", "Allowed browser origins", "Read when the API starts."),
    Spec("job_runner", "restart", "system", "Job runner", "celery (workers) or thread (inside the API process)."),

    Spec("upload_max_mb", "runtime", "files", "Upload limit",
         "One browser upload may not be bigger than this. Cannot exceed the upload ceiling.", 1, None, "MB"),
    Spec("upload_ceiling_mb", "restart", "files", "Upload ceiling",
         "What nginx in front accepts (client_max_body_size), set when the frontend container starts. "
         "Raising it needs .env and a restart of frontend and api.", unit="MB"),
    Spec("remote_max_file_mb", "runtime", "files", "Remote file, at most",
         "One remote file (or one file unpacked from a .zip) may not be bigger than this.", 1, 1_048_576, "MB"),
    Spec("remote_max_files", "runtime", "files", "Files per run, at most",
         "An \"all matching files\" run loads at most this many; the rest wait for the next run.", 1, 100_000, "files"),
    Spec("remote_inspect_full_mb", "runtime", "files", "Preview reads whole files up to",
         "Bigger remote files are sampled for a preview.", 1, 10_240, "MB"),
    Spec("remote_cache_mb", "runtime", "files", "Preview cache",
         "Previews and unpacked archives are cached up to this total, least recently used out.", 64, 1_048_576, "MB"),
    Spec("remote_timeout_s", "runtime", "files", "Remote request timeout",
         "Per request to S3 / SFTP / HTTP; a download may run longer, this is the idle limit.", 5, 3600, "s"),
    Spec("remote_list_max", "runtime", "files", "Listing, at most",
         "A file pattern may scan at most this many entries under its folder.", 100, 10_000_000, "entries"),

    Spec("combined_preview_rows", "runtime", "combined", "Preview rows per input",
         "A combined source's preview stages at most this many rows of each input.", 100, 10_000_000, "rows"),
    Spec("combined_lookup_max_rows", "runtime", "combined", "Stream-mode lookup rows, at most",
         "A lookup input held in memory may not have more rows (use staged mode for bigger ones).",
         1_000, 1_000_000_000, "rows"),
    Spec("combined_lookup_max_mb", "runtime", "combined", "Stream-mode lookup memory, at most",
         "The lookups of one job may not take more memory than this.", 64, 1_048_576, "MB"),
    Spec("combined_min_free_gb", "runtime", "combined", "Keep free on the data disk",
         "Staging stops when free space drops below this, or below the share below, whichever is larger.", 0, 10_000, "GB"),
    Spec("combined_min_free_pct", "runtime", "combined", "Keep free on the data disk (share)",
         "The same floor as a share of the disk.", 0, 90, "%"),

    Spec("monitor_warn_pct", "runtime", "monitor", "High level",
         "CPU, memory or disk above this shows as High, and is the host alert's default threshold.", 50, 99, "%"),
    Spec("check_timeout_s", "runtime", "monitor", "Service check timeout",
         "HTTP, TCP and docker service checks give up after this long.", 1, 120, "s"),
    Spec("connection_slow_ms", "runtime", "monitor", "Slow connection",
         "A connection handshake slower than this shows as degraded.", 50, 60_000, "ms"),
    Spec("check_slow_ms", "runtime", "monitor", "Slow service check",
         "A service check that answers slower than this shows as warn.", 50, 120_000, "ms"),
    Spec("health_check_interval_s", "restart", "monitor", "Connection health checks every",
         "Celery beat builds its schedule when it starts.", unit="s"),
    Spec("monitor_disks", "restart", "monitor", "Extra disks",
         "\"path[:label],…\" as seen inside the container: needs volume mounts."),
    Spec("docker_socket", "restart", "monitor", "Docker socket", "Where docker service checks read container state."),

    Spec("public_url", "runtime", "alerts", "Public URL",
         "Where people open Conduit, e.g. https://conduit.example.com. Alert messages link to the right page; "
         "empty means no links."),
    Spec("instance_name", "runtime", "alerts", "Instance name",
         "Shown in alert messages and exports: tells two Conduits apart."),
    Spec("alert_send_timeout_s", "runtime", "alerts", "Send timeout",
         "One attempt to reach Telegram, Slack, the SMTP server or a webhook.", 2, 120, "s"),

    Spec("database_url", "hidden"), Spec("redis_url", "hidden"), Spec("secret_key", "hidden"),
    Spec("jwt_secret", "hidden"), Spec("admin_user", "hidden"), Spec("admin_password", "hidden"),
    Spec("telegram_api_url", "hidden"),
)
SPECS: dict[str, Spec] = {s.key: s for s in _ALL}
RUNTIME = tuple(k for k, s in SPECS.items() if s.cls == "runtime")


class SettingError(ValueError):
    pass


def kind(key: str) -> type:
    return Settings.model_fields[key].annotation  # int | float | str


def default(key: str) -> Any:
    return Settings.model_fields[key].default


def bounds(key: str) -> tuple[float | None, float | None]:
    s = SPECS[key]
    if key == "upload_max_mb":
        return s.lo, get_settings().upload_ceiling_mb
    return s.lo, s.hi


def _num(n: float) -> str:
    return f"{n:,.0f}" if float(n).is_integer() else f"{n:,}"


def check(key: str, raw: Any) -> Any:
    """A value for a runtime setting, typed and within bounds, or SettingError with a one-line reason."""
    spec = SPECS[key]
    t = kind(key)
    if t is str:
        if not isinstance(raw, str):
            raise SettingError(f"{spec.label}: expected text")
        v = raw.strip()
        if key == "public_url":
            if not v:
                return ""
            u = urlsplit(v)
            if u.scheme not in ("http", "https") or not u.netloc or any(c.isspace() for c in v) or u.query or u.fragment:
                raise SettingError("Public URL: an http(s) address like https://conduit.example.com, or empty")
            return v.rstrip("/")
        if key == "instance_name" and (not 1 <= len(v) <= 60 or "\n" in v):
            raise SettingError(f"{spec.label}: 1 to 60 characters on one line")
        return v
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise SettingError(f"{spec.label}: expected a number")
    try:
        n = float(str(raw).strip().replace(",", "").replace("_", ""))
    except ValueError:
        raise SettingError(f"{spec.label}: expected a number") from None
    if t is int:
        if not n.is_integer():
            raise SettingError(f"{spec.label}: a whole number")
        n = int(n)
    lo, hi = bounds(key)
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        unit = f" {spec.unit}" if spec.unit else ""
        raise SettingError(f"{spec.label}: between {_num(lo)} and {_num(hi)}{unit}")
    return n


# ---- the effective values -------------------------------------------------------------------------------------

_cache: tuple[str, float, dict[str, Any]] | None = None  # (database url, read at, overrides)


def _load() -> dict[str, Any]:
    with get_sessionmaker()() as s:
        rows = s.execute(select(Setting.key, Setting.value).where(Setting.key.in_(RUNTIME))).all()
    out = {}
    for key, value in rows:
        try:
            out[key] = check(key, value)
        except SettingError as e:
            log.warning("ignoring the saved %s override: %s", key, e)
    return out


def overrides() -> dict[str, Any]:
    """The UI overrides, read at most every CACHE_S per process (keyed by the app database, so tests never share)."""
    global _cache
    url, now = get_settings().database_url, time.monotonic()
    c = _cache
    if c is not None and c[0] == url and now - c[1] < CACHE_S:
        return c[2]
    try:
        values = _load()
    except Exception:  # noqa: BLE001 - app database unreachable or not migrated yet: the .env values meanwhile
        log.warning("runtime settings unavailable, using .env values", exc_info=True)
        values = {}
    _cache = (url, now, values)
    return values


def invalidate() -> None:
    global _cache
    _cache = None


def current_settings() -> Settings:
    """get_settings() with the UI overrides applied (a copy: the cached environment view is never changed)."""
    base, ov = get_settings(), overrides()
    if base.upload_max_mb > base.upload_ceiling_mb and "upload_max_mb" not in ov:
        ov = {**ov, "upload_max_mb": base.upload_ceiling_mb}
    return base.model_copy(update=ov) if ov else base


# ---- the Settings page --------------------------------------------------------------------------------------

def describe(session: Session, key: str | None = None) -> list[dict[str, Any]]:
    """Every visible setting (or one): effective value, where it comes from, bounds and the one-line help."""
    env = get_settings()
    keys = [key] if key else [k for k, s in SPECS.items() if s.cls != "hidden"]
    rows = {r.key: r for r in session.scalars(select(Setting).where(Setting.key.in_(keys)))}
    ids = {r.updated_by_id for r in rows.values() if r.updated_by_id}
    names = dict(session.execute(select(User.id, User.username).where(User.id.in_(ids))).all()) if ids else {}
    out = []
    for k in keys:
        spec, d, from_env = SPECS[k], default(k), getattr(env, k)
        env_set = k in env.model_fields_set and from_env != d
        value, source, note = from_env, ("env" if env_set else "default"), None
        row = rows.get(k) if spec.cls == "runtime" else None
        if row is not None:
            try:
                value, source = check(k, row.value), "ui"
            except SettingError as e:
                note, row = f"The saved value {row.value!r} is ignored: {e}", None
        if k == "upload_max_mb" and value > env.upload_ceiling_mb:
            capped = f"capped at the upload ceiling ({env.upload_ceiling_mb:,} MB)"
            value, note = env.upload_ceiling_mb, f"{note}; {capped}" if note else capped[0].upper() + capped[1:]
        lo, hi = bounds(k) if spec.cls == "runtime" else (None, None)
        t = kind(k)
        out.append({
            "key": k, "env": f"CONDUIT_{k.upper()}", "group": spec.group, "group_label": GROUPS[spec.group],
            "label": spec.label, "help": spec.help, "cls": spec.cls,
            "type": "int" if t is int else "float" if t is float else "text", "unit": spec.unit, "min": lo, "max": hi,
            "value": value, "default": d, "env_value": from_env if env_set else None, "source": source, "note": note,
            "updated_by": names.get(row.updated_by_id) if row is not None else None,
            "updated_at": row.updated_at if row is not None else None,
        })
    return out
