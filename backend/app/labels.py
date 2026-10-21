"""Readable names for what a job, pipeline or comparison reads and writes, e.g. `orders (prod-pg) → public.orders (ch)`.

For screens that show a load without its spec (viewers): a table or file name and the connection's name, a masked
URL. Never SQL text, a WHERE filter, a URL's query string or a credential.
"""
import posixpath
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .connectors.http import mask_url
from .models import Connection, User


def connection_names(session: Session) -> dict[str, str]:
    return {str(c.id): c.name for c in session.scalars(select(Connection))}


def usernames(session: Session) -> dict[uuid.UUID, str]:
    return {u.id: u.username for u in session.scalars(select(User))}


def _conn(conns: dict[str, str], cid: Any) -> str:
    return conns.get(str(cid), "deleted connection") if cid else "?"


def _place(side: dict, conns: dict[str, str]) -> str:
    if side.get("file"):
        return "file"
    if side.get("remote"):
        cid = side["remote"].get("connection_id")
        return _conn(conns, cid) if cid else "URL"
    return _conn(conns, side.get("connection_id"))


def source_label(side: dict | None, conns: dict[str, str]) -> str:
    s = side or {}
    if s.get("combined"):
        return " + ".join(f"{i.get('alias')} ({_place(i.get('source') or {}, conns)})" for i in s["combined"].get("inputs") or [])
    if s.get("remote"):
        path = mask_url(s["remote"].get("path") or "")
        return f"{posixpath.basename(path.rstrip('/')) or path} ({_place(s, conns)})"
    if s.get("file"):
        return posixpath.basename(s["file"].get("path") or "") or "file"
    return f"{s.get('table') or ('query' if s.get('query') else '?')} ({_conn(conns, s.get('connection_id'))})"


def target_label(side: dict | None, conns: dict[str, str]) -> str:
    t = side or {}
    return f"{t.get('table') or '?'} ({_conn(conns, t.get('connection_id'))})"
