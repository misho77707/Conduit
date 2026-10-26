import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..runtime import current_settings
from ..connectors import REGISTRY, UnknownConnectorType, category_of, db_types, get_connector
from ..db import get_session
from ..deps import admin_only
from ..models import Connection, User
from ..schemas import (
    ConnectionIn, ConnectionOut, ConnectionPatch, ConnectionTestIn, ConnectionTestOut, TableOut, TableSchemaOut,
    TestResultOut,
)
from ..security import SecretKeyError, decrypt_secrets
from ..services import (
    NeedsCredentials, all_connections, build_connector, serialize, set_secrets, test_connection, test_many,
)

router = APIRouter(prefix="/connections", tags=["connections"], dependencies=[Depends(admin_only)])
meta_router = APIRouter(prefix="/meta", tags=["meta"], dependencies=[Depends(admin_only)])


@meta_router.get("/db-types")
def list_db_types() -> list[dict]:
    """Database types the add-connection flow can offer (label, form kind, default port ...)."""
    return db_types()


def _get_or_404(session: Session, conn_id: uuid.UUID) -> Connection:
    conn = session.get(Connection, conn_id)
    if conn is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")
    return conn


def _database_or_422(conn: Connection) -> None:
    if category_of(conn.type) != "database":
        raise HTTPException(422, f"{conn.name} is a file storage connection ({conn.type}): it has files, not tables")


def _validate(type_: str, config: dict, secrets: dict) -> None:
    cls = REGISTRY.get(type_)
    if cls is None:
        raise HTTPException(422, f"Unsupported database type: {type_}")
    errors = cls.validate(config, secrets)
    if errors:
        raise HTTPException(422, "; ".join(errors))


@router.get("", response_model=list[ConnectionOut])
def list_connections(session: Session = Depends(get_session)) -> list[ConnectionOut]:
    return [serialize(c) for c in all_connections(session)]


@router.post("", response_model=ConnectionOut, status_code=status.HTTP_201_CREATED)
def create_connection(
    body: ConnectionIn, session: Session = Depends(get_session), user: User = Depends(admin_only)
) -> ConnectionOut:
    _validate(body.type, body.config, body.secrets)
    conn = Connection(name=body.name.strip(), type=body.type, env=body.env, config=body.config, owner_id=user.id)
    conn.display_host = get_connector(body.type, body.config, body.secrets).display_host()
    try:
        set_secrets(conn, body.secrets)
    except SecretKeyError as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(e)) from e
    session.add(conn)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"A connection named '{body.name}' already exists") from None
    return serialize(conn)


@router.post("/test", response_model=TestResultOut)
async def test_unsaved(body: ConnectionTestIn) -> TestResultOut:
    """Test settings before saving. Nothing is stored."""
    _validate(body.type, body.config, body.secrets)
    connector = get_connector(body.type, body.config, body.secrets, timeout=current_settings().connect_timeout_s)
    return TestResultOut(**(await run_in_threadpool(connector.test)).to_dict())


@router.post("/test-all", response_model=list[ConnectionTestOut])
async def test_all(session: Session = Depends(get_session)) -> list[ConnectionTestOut]:
    pairs = await run_in_threadpool(lambda: test_many(session, all_connections(session)))
    return [ConnectionTestOut(**r.to_dict(), connection=serialize(c)) for c, r in pairs]


@router.get("/{conn_id}", response_model=ConnectionOut)
def get_connection(conn_id: uuid.UUID, session: Session = Depends(get_session)) -> ConnectionOut:
    return serialize(_get_or_404(session, conn_id))


@router.patch("/{conn_id}", response_model=ConnectionOut)
def update_connection(conn_id: uuid.UUID, body: ConnectionPatch, session: Session = Depends(get_session)) -> ConnectionOut:
    conn = _get_or_404(session, conn_id)
    try:
        secrets = decrypt_secrets(conn.secret_enc)
    except SecretKeyError:
        secrets = {}  # unreadable old secrets are replaced by whatever the caller sends
    config = {**(conn.config or {}), **(body.config or {})}
    if body.secrets:
        secrets = {**secrets, **{k: v for k, v in body.secrets.items() if v not in (None, "")}}
    _validate(conn.type, config, secrets)
    if body.name is not None:
        conn.name = body.name.strip()
    if body.env is not None:
        conn.env = body.env or None
    conn.config = config
    conn.display_host = get_connector(conn.type, config, secrets).display_host()
    set_secrets(conn, secrets)
    if body.secrets and any(v not in (None, "") for v in body.secrets.values()):
        conn.needs_credentials = False  # imported without secrets: usable again once they are given
    conn.status, conn.note = "idle", "Settings changed: not checked yet"
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "That name is already in use") from None
    return serialize(conn)


@router.delete("/{conn_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(conn_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    session.delete(_get_or_404(session, conn_id))
    session.commit()


@router.post("/{conn_id}/test", response_model=ConnectionTestOut)
async def test_saved(conn_id: uuid.UUID, session: Session = Depends(get_session)) -> ConnectionTestOut:
    conn = _get_or_404(session, conn_id)
    result = await run_in_threadpool(test_connection, session, conn)
    return ConnectionTestOut(**result.to_dict(), connection=serialize(conn))


@router.get("/{conn_id}/tables", response_model=list[TableOut])
async def list_tables(conn_id: uuid.UUID, session: Session = Depends(get_session)) -> list[TableOut]:
    conn = _get_or_404(session, conn_id)
    _database_or_422(conn)
    try:
        connector = build_connector(conn)
        tables = await run_in_threadpool(connector.list_tables)
    except NeedsCredentials as e:
        raise HTTPException(422, str(e)) from e
    except (SecretKeyError, UnknownConnectorType) as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(e)) from e
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"{type(e).__name__}: {e}"[:400]) from e
    return [TableOut(**t.to_dict()) for t in tables]


@router.get("/{conn_id}/tables/{table}/inspect", response_model=TableSchemaOut)
async def inspect_table(conn_id: uuid.UUID, table: str, session: Session = Depends(get_session)) -> TableSchemaOut:
    conn = _get_or_404(session, conn_id)
    _database_or_422(conn)
    try:
        connector = build_connector(conn)
        schema = await run_in_threadpool(connector.inspect, table)
    except NeedsCredentials as e:
        raise HTTPException(422, str(e)) from e
    except (SecretKeyError, UnknownConnectorType) as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(e)) from e
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"{type(e).__name__}: {e}"[:400]) from e
    return TableSchemaOut(**schema.to_dict())
