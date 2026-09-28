"""Connection logic shared by the API and the worker (encryption, testing, state updates)."""
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .connectors import Connector, get_connector
from .connectors.base import TestResult
from .models import Connection, utcnow
from .schemas import ConnectionOut
from .security import SecretKeyError, decrypt_secrets, encrypt_secrets


def build_connector(conn: Connection) -> Connector:
    secrets = decrypt_secrets(conn.secret_enc)
    return get_connector(conn.type, conn.config or {}, secrets, timeout=get_settings().connect_timeout_s)


def serialize(conn: Connection) -> ConnectionOut:
    return ConnectionOut(
        id=conn.id, name=conn.name, type=conn.type, env=conn.env, host=conn.display_host,
        database=(conn.config or {}).get("database"), config=conn.config or {},
        status=conn.status, latency_ms=conn.latency_ms, server_version=conn.server_version,
        note=conn.note, last_checked_at=conn.last_checked_at, created_at=conn.created_at,
    )


def set_secrets(conn: Connection, secrets: dict[str, Any]) -> None:
    conn.secret_enc = encrypt_secrets({k: v for k, v in secrets.items() if v not in (None, "")})


def apply_result(conn: Connection, result: TestResult) -> None:
    """Overwrite the connection's current state. History is intentionally not kept."""
    conn.status = result.status
    conn.latency_ms = result.latency_ms
    conn.last_checked_at = utcnow()
    if result.ok:
        conn.server_version = result.version or conn.server_version
        conn.note = "Slow handshake" if result.degraded else None
    else:
        conn.note = (result.error or "Check failed")[:500]


def _test_one(conn: Connection) -> TestResult:
    try:
        return build_connector(conn).test()
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
