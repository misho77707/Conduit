"""Monitor: current state of connections, running jobs, host resources and service checks. Nothing here is history."""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..deps import current_user
from ..engine.runner import snapshot as job_snapshot
from ..models import Connection, Job, ServiceCheck, User, utcnow
from ..monitor import checks as chk
from ..monitor import host
from ..connectors import category_of
from ..services import all_connections, serialize, test_many

router = APIRouter(prefix="/monitor", tags=["monitor"], dependencies=[Depends(current_user)])

MIN_INTERVAL_S, MAX_INTERVAL_S = 10, 3600
_NO_SQL = ("mongodb",)  # query checks need a SQL database


# ---- request / response models --------------------------------------------------------------------------------

class CheckIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["http", "tcp", "query", "docker"]
    target: str = Field("", max_length=500)  # http: URL; tcp: host:port; docker: container name or ID
    connection_id: uuid.UUID | None = None  # query
    query: str | None = Field(None, max_length=4000)
    expect: str = Field("", max_length=20)  # http: status code; tcp: max connect ms; query: expected rows; docker: "running"/"healthy"
    interval_s: int = Field(30, ge=MIN_INTERVAL_S, le=MAX_INTERVAL_S)
    verify_tls: bool = True
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self) -> "CheckIn":
        self.name, self.target, self.expect = self.name.strip(), self.target.strip(), self.expect.strip()
        if not self.name:
            raise ValueError("Give the check a name")
        if self.kind != "docker" and self.expect and not self.expect.isdigit():
            raise ValueError("Expected value must be a whole number")
        if self.kind == "http":
            u = self.target.lower()
            if not (u.startswith("http://") or u.startswith("https://")) or len(u.split("//", 1)[1].split("/")[0]) == 0:
                raise ValueError("URL must start with http:// or https://")
            if self.expect and not 100 <= int(self.expect) <= 599:
                raise ValueError("Expected status must be between 100 and 599")
        elif self.kind == "tcp":
            chk.split_host_port(self.target)
            if self.expect and int(self.expect) < 1:
                raise ValueError("Max latency must be at least 1 ms")
        elif self.kind == "docker":
            if not self.target:
                raise ValueError("Enter the container name or ID")
            if self.expect not in ("", "running", "healthy"):
                raise ValueError('Expected must be "running", "healthy", or left blank')
        else:
            if self.connection_id is None:
                raise ValueError("Pick the connection to run the query on")
            q = (self.query or "").strip().rstrip(";").strip()
            if not q:
                raise ValueError("Enter a query")
            if ";" in q:
                raise ValueError("Use a single statement")
            if q.split(None, 1)[0].lower() not in ("select", "with"):
                raise ValueError("Only SELECT (or WITH ... SELECT) queries can be used")
            self.query, self.target = q, ""
        return self


class CheckPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    target: str | None = Field(None, max_length=500)
    connection_id: uuid.UUID | None = None
    query: str | None = Field(None, max_length=4000)
    expect: str | None = Field(None, max_length=20)
    interval_s: int | None = Field(None, ge=MIN_INTERVAL_S, le=MAX_INTERVAL_S)
    verify_tls: bool | None = None
    enabled: bool | None = None


class CheckOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    target: str  # what to show: the URL, host:port, or "<connection> · <query>"
    connection_id: uuid.UUID | None
    query: str | None
    expect: str
    interval_s: int
    verify_tls: bool
    enabled: bool
    status: str  # pending | up | warn | down
    message: str
    latency_ms: float | None
    checked_at: datetime | None
    stale: bool  # enabled but not checked for a long time: is beat running?


def _stale(c: ServiceCheck) -> bool:
    if not c.enabled or c.checked_at is None:
        return False
    return (utcnow() - chk._aware(c.checked_at)).total_seconds() > c.interval_s * 3 + 30


def _out(c: ServiceCheck, conn_names: dict[uuid.UUID, str]) -> CheckOut:
    target = c.target
    if c.kind == "query":
        target = f"{conn_names.get(c.connection_id, 'deleted connection')} · {' '.join((c.query or '').split())}"
    return CheckOut(id=c.id, name=c.name, kind=c.kind, target=target, connection_id=c.connection_id, query=c.query,
                    expect=c.expect, interval_s=c.interval_s, verify_tls=c.verify_tls, enabled=c.enabled, status=c.status,
                    message=c.message, latency_ms=c.latency_ms, checked_at=c.checked_at, stale=_stale(c))


def _names(session: Session) -> dict[uuid.UUID, str]:
    return {i: n for i, n in session.execute(select(Connection.id, Connection.name))}


def _validated_connection(session: Session, connection_id: uuid.UUID | None) -> None:
    conn = session.get(Connection, connection_id) if connection_id else None
    if conn is None:
        raise HTTPException(422, "Connection not found")
    if category_of(conn.type) != "database":
        raise HTTPException(422, f"Query checks need a SQL database ({conn.name} is a file storage connection)")
    if conn.type in _NO_SQL:
        raise HTTPException(422, f"Query checks need a SQL database ({conn.type} is not supported)")


def _get_check(session: Session, check_id: uuid.UUID) -> ServiceCheck:
    c = session.get(ServiceCheck, check_id)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Check not found")
    return c


# ---- service checks -------------------------------------------------------------------------------------------

@router.get("/checks", response_model=list[CheckOut])
def list_checks(session: Session = Depends(get_session)) -> list[CheckOut]:
    names = _names(session)
    return [_out(c, names) for c in session.scalars(select(ServiceCheck).order_by(ServiceCheck.created_at))]


@router.post("/checks", response_model=CheckOut, status_code=status.HTTP_201_CREATED)
def create_check(body: CheckIn, session: Session = Depends(get_session), user: User = Depends(current_user)) -> CheckOut:
    if body.kind == "query":
        _validated_connection(session, body.connection_id)
    c = ServiceCheck(name=body.name, kind=body.kind, target=body.target, connection_id=body.connection_id if body.kind == "query" else None,
                     query=body.query if body.kind == "query" else None, expect=body.expect, interval_s=body.interval_s, verify_tls=body.verify_tls,
                     enabled=body.enabled, owner_id=user.id)
    session.add(c)
    session.commit()
    if c.enabled:  # first result in a moment instead of waiting for the next beat tick
        threading.Thread(target=chk.run_and_store, args=(c.id,), daemon=True, name=f"check-{str(c.id)[:8]}").start()
    return _out(c, _names(session))


@router.patch("/checks/{check_id}", response_model=CheckOut)
def update_check(check_id: uuid.UUID, body: CheckPatch, session: Session = Depends(get_session)) -> CheckOut:
    c = _get_check(session, check_id)
    data = body.model_dump(exclude_unset=True)
    merged = CheckIn(name=data.get("name", c.name), kind=c.kind, target=data.get("target", c.target),
                     connection_id=data.get("connection_id", c.connection_id), query=data.get("query", c.query),
                     expect=data.get("expect", c.expect), interval_s=data.get("interval_s", c.interval_s),
                     verify_tls=data.get("verify_tls", c.verify_tls), enabled=data.get("enabled", c.enabled))  # re-validate
    if c.kind == "query":
        _validated_connection(session, merged.connection_id)
    changed_what = any(k in data for k in ("target", "connection_id", "query", "expect", "verify_tls"))
    switched_on = bool(data.get("enabled")) and not c.enabled
    for field in ("name", "target", "connection_id", "query", "expect", "interval_s", "verify_tls", "enabled"):
        setattr(c, field, getattr(merged, field))
    if changed_what:  # the old result no longer describes this check
        c.status, c.message, c.latency_ms, c.checked_at = "pending", "waiting for first check", None, None
    session.commit()
    if c.enabled and (changed_what or switched_on):
        threading.Thread(target=chk.run_and_store, args=(c.id,), daemon=True, name=f"check-{str(c.id)[:8]}").start()
    return _out(c, _names(session))


@router.delete("/checks/{check_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_check(check_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    session.delete(_get_check(session, check_id))
    session.commit()


@router.post("/checks/{check_id}/run", response_model=CheckOut)
def run_check_now(check_id: uuid.UUID, session: Session = Depends(get_session)) -> CheckOut:
    c = _get_check(session, check_id)
    if not chk.run_and_store(c.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "This check is already running")
    session.refresh(c)
    return _out(c, _names(session))


# ---- the snapshot ---------------------------------------------------------------------------------------------

_slots_cache: tuple[float, tuple[int | None, bool | None]] = (0.0, (None, None))


def worker_slots() -> tuple[int | None, bool | None]:
    """(total worker slots, any worker online). (None, None) when it cannot be known (jobs run in-process, broker down)."""
    global _slots_cache
    if get_settings().job_runner == "thread":
        return None, None
    ts, val = _slots_cache
    if time.monotonic() - ts < 10:
        return val
    try:
        from ..worker.celery_app import celery_app

        stats = celery_app.control.inspect(timeout=1.0).stats() or {}
        total = sum(int((v.get("pool") or {}).get("max-concurrency") or 0) for v in stats.values())
        val = (total, bool(stats))
    except Exception:  # noqa: BLE001 - broker unreachable
        val = (None, None)
    _slots_cache = (time.monotonic(), val)
    return val


def build_snapshot(session: Session) -> dict:
    conns = [serialize(c) for c in all_connections(session)]
    jobs = list(session.scalars(select(Job).where(Job.status.in_(("queued", "planning", "running"))).order_by(Job.created_at)))
    active = [j for j in jobs if j.status in ("planning", "running")]
    slots_total, online = worker_slots()
    check_rows = list(session.scalars(select(ServiceCheck).order_by(ServiceCheck.created_at)))
    names = _names(session)
    services = [_out(c, names) for c in check_rows]
    n = lambda seq, key, val: sum(1 for x in seq if getattr(x, key) == val)  # noqa: E731
    return {
        "updated_at": utcnow(),
        "connections": [c.model_dump(mode="json") for c in conns],
        "host": host.snapshot(),
        "jobs": {
            "running": len(active), "queued": len(jobs) - len(active), "slots_total": slots_total, "worker_online": online,
            "active": [job_snapshot(j) for j in active],
        },
        "services": [s.model_dump(mode="json") for s in services],
        "summary": {
            "connections": {"up": n(conns, "status", "ok"), "warn": n(conns, "status", "warn"), "down": n(conns, "status", "err"),
                            "unchecked": n(conns, "status", "idle"), "total": len(conns)},
            "services": {"up": n(services, "status", "up"), "warn": n(services, "status", "warn"),
                         "down": n(services, "status", "down"), "pending": n(services, "status", "pending"), "total": len(services)},
        },
    }


@router.get("")
def monitor(session: Session = Depends(get_session)) -> dict:
    """Everything the Monitor screen shows, in one call. Poll it every few seconds."""
    return _jsonable(build_snapshot(session))


def _jsonable(obj):
    import json

    return json.loads(json.dumps(obj, default=lambda o: o.isoformat() if hasattr(o, "isoformat") else str(o)))


def _refresh(session: Session) -> dict:
    chk.run_all_now()
    test_many(session, all_connections(session))
    session.expire_all()
    return _jsonable(build_snapshot(session))


@router.post("/refresh")
async def refresh(session: Session = Depends(get_session)) -> dict:
    """Test every connection and run every service check now, then return the fresh snapshot."""
    return await run_in_threadpool(_refresh, session)
