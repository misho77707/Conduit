"""Open a SourceIn (saved connection table / query, or a file) for reading, outside a load job.

Shared by the pipeline preview and the compare engine. Failures raise JobError (a request problem, 422) or its
subclass SourceUnavailable (the database / driver failed, 502).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..connectors import category_of
from ..models import Connection
from ..services import build_connector
from .adapters import get_adapter
from .filesource import FileAdapter, FileSpec
from .runner import JobError, short_error
from .spec import SourceIn
from .types import ColSpec


class SourceUnavailable(JobError):
    """The source exists in the request but could not be reached or probed."""


def source_connection(session: Session, s: SourceIn, role: str = "source") -> Connection:
    conn = session.get(Connection, s.connection_id) if s.connection_id else None
    if conn is None:
        raise JobError(f"The {role} connection no longer exists")
    if category_of(conn.type) != "database":
        raise JobError(f"{conn.name} is a file storage connection ({conn.type}): it has files, not tables")
    return conn


def source_adapter(session: Session, s: SourceIn, role: str = "source") -> Any:
    if s.file:
        try:
            return FileAdapter(FileSpec(**s.file.model_dump()))
        except (FileNotFoundError, ValueError) as e:
            raise JobError(str(e)) from e
    return get_adapter(build_connector(source_connection(session, s, role)))


def raw_columns(src: Any, s: SourceIn, role: str = "source") -> list[ColSpec]:
    """The columns a source yields, as its batches will carry them."""
    if isinstance(src, FileAdapter):
        return src.read_columns()
    if s.table:
        try:
            cols = src.table_columns(s.table)
        except Exception as e:  # noqa: BLE001 - driver / network error while probing the source
            raise SourceUnavailable(f"Could not read the columns of {s.table}: {short_error(e)}") from e
        if not cols:
            raise JobError(f"{role.capitalize()} table {s.table} was not found (or has no columns)")
        return cols
    try:
        return src.query_columns(s.query or "")
    except NotImplementedError as e:
        raise JobError(str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise SourceUnavailable(f"The {role} query does not run: {short_error(e)}") from e
