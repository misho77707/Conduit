"""Remote files (S3 / SFTP / HTTP): browse a storage connection, see what a pattern matches, inspect a file.

Nothing here downloads a big file: inspect previews from the head of a text file or the footer + first rows of
a Parquet file (app/remote.py). Errors from the remote side come back as 422 with a message written for people.
"""
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import remote as rmt
from ..connectors.base import UserFacingError
from ..connectors.storage import RemoteNotFound
from ..db import get_session
from ..deps import admin_only
from ..engine.spec import RemoteIn
from ..files import UnsupportedFormat, format_from_name
from ..schemas import FileInspectOut

router = APIRouter(prefix="/remote", tags=["remote"], dependencies=[Depends(admin_only)])


class TargetRef(BaseModel):
    """The load's target, so already-loaded files can be marked (and Replace + all can be explained)."""

    connection_id: uuid.UUID
    table: str
    mode: Literal["append", "replace", "upsert"] = "append"


class MatchIn(BaseModel):
    remote: RemoteIn
    target: TargetRef | None = None


class InspectIn(BaseModel):
    remote: RemoteIn
    sample_rows: int = Field(20, ge=1, le=1000)
    target: TargetRef | None = None


def _run(fn, *args):
    try:
        return fn(*args)
    except RemoteNotFound as e:
        raise HTTPException(404, str(e)) from e
    except (UserFacingError, UnsupportedFormat) as e:
        raise HTTPException(422, str(e)) from e


def _target(t: TargetRef | None) -> tuple[str | None, str | None]:
    return (rmt.target_key(t), t.mode) if t else (None, None)


@router.get("/{connection_id}/list")
async def list_folder(connection_id: uuid.UUID, path: str = Query(""), session: Session = Depends(get_session)) -> list[dict]:
    """One folder of a storage connection: folders first, then files with size, modified time, format."""
    def go() -> list[dict]:
        c, conn = rmt.storage(session, connection_id)
        entries = c.list(path)
        rmt.remember(session, conn, c)
        out = []
        for e in entries:
            fmt = comp = None
            if not e.is_dir:
                try:
                    fmt, comp = format_from_name(e.name)
                except UnsupportedFormat:
                    pass
            out.append({"name": e.name, "path": e.path, "is_dir": e.is_dir, "size": e.size, "modified": e.modified,
                        "format": fmt, "compression": comp})
        return out

    return await run_in_threadpool(_run, go)


@router.post("/match")
async def match(body: MatchIn, session: Session = Depends(get_session)) -> dict:
    """The files a run would pick right now: totals, the newest one, which are new / changed / already loaded."""
    tkey, mode = _target(body.target)
    return await run_in_threadpool(_run, rmt.match_summary, session, body.remote, tkey, mode)


@router.post("/inspect")
async def inspect(body: InspectIn, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Columns, row count and a preview of the (first) matching file, plus the match summary."""
    tkey, mode = _target(body.target)
    res, summary, file = await run_in_threadpool(_run, rmt.inspect, session, body.remote, body.sample_rows, tkey, mode)
    return {**FileInspectOut(**res.to_dict()).model_dump(), "remote": {"file": file, "match": summary}}
