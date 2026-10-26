"""Service checks: run one, run the ones that are due. Only the latest result is stored (no history)."""
from __future__ import annotations

import http
import http.client
import json
import logging
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import timedelta

from sqlalchemy import case, select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_sessionmaker
from ..models import Connection, ServiceCheck, utcnow

log = logging.getLogger("conduit.monitor")

SLOW_MS = 2000  # answered, but slower than this: "warn"
QUERY_TIMEOUT_S = 15
STALE_CLAIM_S = 60  # a run that claimed a check and never finished (killed worker) is retried after this


class Outcome(tuple):
    """(status, message, latency_ms)"""

    def __new__(cls, status: str, message: str, latency_ms: float | None = None):
        return super().__new__(cls, (status, message[:300], None if latency_ms is None else round(latency_ms, 1)))


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000


def _reason(e: BaseException) -> str:
    """Short, human reason for a network error."""
    if isinstance(e, urllib.error.URLError) and isinstance(e.reason, BaseException):
        e = e.reason
    if isinstance(e, (TimeoutError, socket.timeout)):
        return "timed out"
    if isinstance(e, ConnectionRefusedError):
        return "connection refused"
    if isinstance(e, socket.gaierror):
        return "name not resolved"
    if isinstance(e, ssl.SSLError):
        return f"TLS error: {getattr(e, 'reason', None) or e}"
    if isinstance(e, ConnectionResetError):
        return "connection reset"
    if isinstance(e, OSError) and e.strerror:
        return e.strerror.lower()
    return str(e) or type(e).__name__


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # a 3xx is an answer like any other: compare it with the expected status
        return None


def check_http(check) -> Outcome:
    expect = int(check.expect or 200)
    ctx = ssl.create_default_context()
    if not check.verify_tls:
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    req = urllib.request.Request(check.target, headers={"User-Agent": "conduit-monitor"})
    t0 = time.perf_counter()
    try:
        with opener.open(req, timeout=get_settings().check_timeout_s) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:  # 3xx / 4xx / 5xx
        code = e.code
    except Exception as e:  # noqa: BLE001
        return Outcome("down", _reason(e))
    lat = _ms(t0)
    phrase = http.HTTPStatus(code).phrase if code in http.HTTPStatus._value2member_map_ else ""
    label = f"{code} {phrase}".strip()
    if code != expect:
        return Outcome("down", f"{label}, expected {expect}", lat)
    if lat > SLOW_MS:
        return Outcome("warn", f"{label}, slow", lat)
    return Outcome("up", label, lat)


def split_host_port(target: str) -> tuple[str, int]:
    host, sep, port = target.strip().rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError("Use host:port, for example kafka-1.internal:9092")
    return host.strip("[]"), int(port)


def check_tcp(check) -> Outcome:
    host, port = split_host_port(check.target)
    limit = float(check.expect or 1000)
    t0 = time.perf_counter()
    try:
        socket.create_connection((host, port), timeout=get_settings().check_timeout_s).close()
    except Exception as e:  # noqa: BLE001
        return Outcome("down", _reason(e))
    lat = _ms(t0)
    if lat > limit:
        return Outcome("warn", f"port open, slow ({lat:.0f} ms, limit {limit:.0f} ms)", lat)
    return Outcome("up", "port open", lat)


def _count_rows(connection_id, sql: str) -> int:
    from ..engine.adapters import get_adapter
    from ..engine.model import ReadRequest
    from ..services import build_connector

    with get_sessionmaker()() as s:
        conn = s.get(Connection, connection_id)
        if conn is None:
            raise LookupError("the connection was deleted")
        connector = build_connector(conn)
    n, _ = get_adapter(connector).estimate_rows(ReadRequest(query=sql), exact=True)
    return int(n or 0)


def check_query(check) -> Outcome:
    t0 = time.perf_counter()
    pool = ThreadPoolExecutor(1, thread_name_prefix="conduit-qcheck")
    fut = pool.submit(_count_rows, check.connection_id, check.query or "")
    try:
        n = fut.result(timeout=QUERY_TIMEOUT_S)
    except FutureTimeout:
        return Outcome("down", f"timed out after {QUERY_TIMEOUT_S} s")
    except LookupError as e:
        return Outcome("down", str(e))
    except Exception as e:  # noqa: BLE001
        from ..engine.runner import short_error

        return Outcome("down", short_error(e))
    finally:
        pool.shutdown(wait=False)
    lat = _ms(t0)
    label = f"{n} row" + ("" if n == 1 else "s")
    if check.expect != "" and n != int(check.expect):
        return Outcome("down", f"{label}, expected {int(check.expect)}", lat)
    if lat > SLOW_MS:
        return Outcome("warn", f"{label}, slow", lat)
    return Outcome("up", label, lat)


class _UnixHTTPConnection(http.client.HTTPConnection):
    """http.client, but dialing a unix domain socket instead of TCP - for the Docker Engine API."""

    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._unix_path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._unix_path)


def _docker_get(path: str) -> tuple[int, dict | None]:
    """GET a path from the Docker Engine API over its unix socket. Returns (status, parsed JSON body or None)."""
    conn = _UnixHTTPConnection(get_settings().docker_socket, get_settings().check_timeout_s)
    try:
        conn.request("GET", path, headers={"Host": "localhost"})
        resp = conn.getresponse()
        raw = resp.read()
    finally:
        conn.close()
    try:
        return resp.status, (json.loads(raw) if raw else None)
    except ValueError:
        return resp.status, None


def check_docker(check) -> Outcome:
    """target: a container name or ID. expect: "" / "running" (default - just up) or "healthy" (require a passing
    HEALTHCHECK). Reads current state only, straight from the Docker Engine API - no docker CLI involved."""
    t0 = time.perf_counter()
    socket_path = get_settings().docker_socket
    try:
        code, body = _docker_get(f"/containers/{urllib.parse.quote(check.target, safe='')}/json")
    except FileNotFoundError:
        return Outcome("down", f"docker socket not found at {socket_path} (is it mounted?)")
    except PermissionError:
        return Outcome("down", f"permission denied on {socket_path}")
    except Exception as e:  # noqa: BLE001
        return Outcome("down", _reason(e))
    lat = _ms(t0)
    if code == 404:
        return Outcome("down", "container not found")
    if code != 200 or not isinstance(body, dict):
        return Outcome("down", f"docker API returned {code}")
    state = body.get("State") or {}
    status_str = state.get("Status", "unknown")
    health = (state.get("Health") or {}).get("Status")  # starting | healthy | unhealthy, or absent (no HEALTHCHECK)
    if check.expect == "healthy":
        if not health:
            return Outcome("down", "no healthcheck defined for this container")
        if health == "healthy":
            return Outcome("up", "healthy", lat)
        if health == "starting":
            return Outcome("warn", "health check starting", lat)
        return Outcome("down", "unhealthy" if health == "unhealthy" else f"status: {status_str}", lat)
    if status_str != "running":
        return Outcome("down", f"status: {status_str}", lat)
    if health == "unhealthy":
        return Outcome("warn", "running, but unhealthy", lat)
    return Outcome("up", "running", lat)


RUNNERS = {"http": check_http, "tcp": check_tcp, "query": check_query, "docker": check_docker}


def run_check(check) -> Outcome:
    try:
        return RUNNERS[check.kind](check)
    except Exception as e:  # noqa: BLE001 - a broken check must never take the runner down
        log.exception("service check %s crashed", check.id)
        return Outcome("down", f"check failed to run: {type(e).__name__}: {e}")


# ---- claiming and storing ----------------------------------------------------------

def _aware(d):
    return d if d is None or d.tzinfo else d.replace(tzinfo=utcnow().tzinfo)


def is_due(c: ServiceCheck, now=None) -> bool:
    now = now or utcnow()
    if not c.enabled:
        return False
    claimed = _aware(c.claimed_at)
    if claimed and (now - claimed).total_seconds() < STALE_CLAIM_S:
        return False  # someone is running it right now
    last = _aware(c.checked_at)
    return last is None or (now - last).total_seconds() >= c.interval_s


def _claim(session: Session, check_id) -> bool:
    """Atomically mark a check as being run, so overlapping runners never run it twice."""
    now = utcnow()
    stale = now - timedelta(seconds=STALE_CLAIM_S)
    res = session.execute(update(ServiceCheck).where(
        ServiceCheck.id == check_id, (ServiceCheck.claimed_at.is_(None)) | (ServiceCheck.claimed_at < stale))
        .values(claimed_at=now))
    session.commit()
    return res.rowcount == 1


def run_and_store(check_id) -> bool:
    """Run one check now (skipped when another runner holds it) and save its result. True if it ran."""
    Session = get_sessionmaker()
    with Session() as s:
        if not _claim(s, check_id):
            return False
        c = s.get(ServiceCheck, check_id)
        if c is None:
            return False
        snap = SimpleNamespace(id=c.id, kind=c.kind, target=c.target, expect=c.expect, connection_id=c.connection_id,
                               query=c.query, verify_tls=c.verify_tls)
        s.rollback()  # do not sit in a transaction while the network call runs
    status, message, lat = run_check(snap)
    now = utcnow()
    down = status == "down"
    with Session() as s:
        s.execute(update(ServiceCheck).where(ServiceCheck.id == check_id).values(
            status=status, message=message, latency_ms=lat, checked_at=now, claimed_at=None,
            # consecutive `down` runs and since when, for "check down for K runs" alerts (old values on the right)
            fail_streak=(ServiceCheck.fail_streak + 1) if down else 0,
            fail_since=case((ServiceCheck.fail_streak > 0, ServiceCheck.fail_since), else_=now) if down else None))
        s.commit()
    return True


def run_due(max_workers: int = 8) -> int:
    """Run every enabled check whose interval has passed. Called by beat every few seconds."""
    with get_sessionmaker()() as s:
        due = [c.id for c in s.scalars(select(ServiceCheck)) if is_due(c)]
    if not due:
        return 0
    with ThreadPoolExecutor(max_workers=min(max_workers, len(due)), thread_name_prefix="conduit-check") as pool:
        return sum(1 for ran in pool.map(run_and_store, due) if ran)


def run_all_now(max_workers: int = 8) -> int:
    """Run every enabled check right now, whatever its interval (the Refresh button)."""
    with get_sessionmaker()() as s:
        ids = [c.id for c in s.scalars(select(ServiceCheck).where(ServiceCheck.enabled.is_(True)))]
    if not ids:
        return 0
    with ThreadPoolExecutor(max_workers=min(max_workers, len(ids)), thread_name_prefix="conduit-check") as pool:
        return sum(1 for ran in pool.map(run_and_store, ids) if ran)
