"""Connection logic shared by the API and the worker (encryption, testing, state updates)."""
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .runtime import current_settings
from .connectors import Connector, category_of, get_connector
from .connectors.base import TestResult, UserFacingError
from .models import Connection, utcnow
from .schemas import ConnectionOut
from .security import SecretKeyError, decrypt_secrets, encrypt_secrets


NEEDS_NOTE = "Needs credentials: edit it and add its secrets"


class NeedsCredentials(UserFacingError, ValueError):
    pass


def build_connector(conn: Connection) -> Connector:
    if conn.needs_credentials:
        raise NeedsCredentials(f"{conn.name} needs credentials: it was imported without its secrets. Edit the "
                               "connection and add them first.")
    secrets = decrypt_secrets(conn.secret_enc)
    connector = get_connector(conn.type, conn.config or {}, secrets, timeout=current_settings().connect_timeout_s)
    connector.driver_pref = conn.driver  # the adapter's default driver choice (engine/drivers.py)
    return connector


def driver_note(conn: Connection, connector: Connector) -> str | None:
    """Health check: when the connection's driver is a fast one, can it actually be opened? (catalog work and the
    test itself always use the classic driver)."""
    from .engine.adapters import get_adapter
    from .engine.drivers import AUTO, importable, options_of

    opts = options_of(connector)
    if not opts or conn.driver is None:
        return None
    classic = next(o for o in opts if o.classic)
    fast = next((o for o in opts if not o.classic and (conn.driver == AUTO or o.id == conn.driver)), None)
    if fast is None:
        return None
    then = f"jobs use {classic.label}" if conn.driver == AUTO else "jobs on this connection are refused"
    if fast.module and not importable(fast.module):
        return f"{fast.label}: the {fast.module} package is not installed; {then}"
    try:
        err = get_adapter(connector).probe_driver(fast.id)
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    return f"{fast.label} can't connect ({' '.join(str(err).split())[:200]}); {then}" if err else None


def serialize(conn: Connection) -> ConnectionOut:
    return ConnectionOut(
        id=conn.id, name=conn.name, type=conn.type, env=conn.env, host=conn.display_host, category=category_of(conn.type),
        database=(conn.config or {}).get("database"), config=conn.config or {},
        status=conn.status, latency_ms=conn.latency_ms, server_version=conn.server_version,
        note=conn.note, last_checked_at=conn.last_checked_at, created_at=conn.created_at,
        needs_credentials=conn.needs_credentials, driver=conn.driver,
    )


def set_secrets(conn: Connection, secrets: dict[str, Any]) -> None:
    conn.secret_enc = encrypt_secrets({k: v for k, v in secrets.items() if v not in (None, "")})


def apply_result(conn: Connection, result: TestResult) -> None:
    """Overwrite the connection's current state. History is intentionally not kept."""
    now = utcnow()
    if conn.needs_credentials:  # never tried: not "down" either (no streak, so no alert)
        conn.status, conn.note, conn.last_checked_at = "idle", NEEDS_NOTE, now
        conn.err_streak, conn.err_since, conn.bad_streak, conn.bad_since = 0, None, 0, None
        return
    # consecutive bad checks (and since when), for "connection down for K checks" alerts
    if result.status == "err":
        conn.err_since = conn.err_since if conn.err_streak else now
        conn.err_streak = (conn.err_streak or 0) + 1
    else:
        conn.err_streak, conn.err_since = 0, None
    if result.status != "ok":
        conn.bad_since = conn.bad_since if conn.bad_streak else now
        conn.bad_streak = (conn.bad_streak or 0) + 1
    else:
        conn.bad_streak, conn.bad_since = 0, None
    conn.status = result.status
    conn.latency_ms = result.latency_ms
    conn.last_checked_at = now
    if result.ok:
        conn.server_version = result.version or conn.server_version
        conn.note = result.note or ("Slow handshake" if result.degraded else None)
        if result.learned:  # e.g. an SFTP host key seen for the first time (trust on first use)
            conn.config = {**(conn.config or {}), **result.learned}
    else:
        conn.note = (result.error or "Check failed")[:500]


def _test_one(conn: Connection) -> TestResult:
    if conn.needs_credentials:
        return TestResult(ok=False, error=NEEDS_NOTE)
    try:
        connector = build_connector(conn)
        result = connector.test()
        if result.ok:
            result.note = driver_note(conn, connector)
        return result
    except SecretKeyError as e:
        return TestResult(ok=False, error=str(e))
    except Exception as e:
        return TestResult(ok=False, error=f"{type(e).__name__}: {e}"[:400])


def test_connection(session: Session, conn: Connection) -> TestResult:
    result = _test_one(conn)
    apply_result(conn, result)
    session.commit()
    return result


def test_many(session: Session, conns: list[Connection], max_workers: int = 8) -> list[tuple[Connection, TestResult]]:
    """Test connections concurrently (network bound), then write state back from this thread."""
    if not conns:
        return []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(conns))) as pool:
        results = list(pool.map(_test_one, conns))
    for conn, res in zip(conns, results):
        apply_result(conn, res)
    session.commit()
    return list(zip(conns, results))


def all_connections(session: Session) -> list[Connection]:
    return list(session.scalars(select(Connection).order_by(Connection.created_at)))
