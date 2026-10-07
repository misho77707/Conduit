"""Compare API: source vs target diffs. A comparison runs in the background like a load job; its result sets stay
on disk (latest few only) for paged samples and exports."""
import shutil
import threading
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from ..compare import export as cexport
from ..compare.engine import FINISHED, describe, folder, is_stalled, resolve, run_comparison, snapshot
from ..compare.spec import CompareIn, SidesIn
from ..config import get_settings
from ..db import get_session
from ..deps import current_user
from ..engine.runner import JobError, short_error
from ..engine.sources import SourceUnavailable
from ..models import Comparison, User, utcnow

router = APIRouter(prefix="/compare", tags=["compare"])


def _or_404(session: Session, cid: uuid.UUID) -> Comparison:
    c = session.get(Comparison, cid)
    if c is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Comparison not found")
    return c


def _request_error(e: JobError) -> HTTPException:
    return HTTPException(status.HTTP_502_BAD_GATEWAY if isinstance(e, SourceUnavailable) else 422, str(e))


def _label(side: Any) -> str:
    return side.table or ("file " + side.file.path.rsplit("/", 1)[-1] if side.file else "query")


@router.post("/columns", dependencies=[Depends(current_user)])
async def columns(body: SidesIn, session: Session = Depends(get_session)) -> dict:
    """Both sides' columns, the automatic pairing (same names) and the suggested key, for the setup screen."""
    try:
        return await run_in_threadpool(describe, session, body.source, body.target)
    except JobError as e:
        raise _request_error(e) from e


def _dispatch(c: Comparison, session: Session) -> None:
    if get_settings().job_runner == "thread":
        threading.Thread(target=run_comparison, args=(c.id,), daemon=True, name=f"cmp-{str(c.id)[:8]}").start()
        return
    from ..worker.tasks import run_comparison_task

    try:
        c.celery_id = run_comparison_task.apply_async(args=[str(c.id)], retry=False).id
        session.commit()
    except Exception as e:  # noqa: BLE001 - broker down
        c.status, c.error, c.finished_at = "failed", f"Could not queue the comparison: {short_error(e)}", utcnow()
        session.commit()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The job queue (Redis) is not reachable") from e


@router.post("", status_code=status.HTTP_201_CREATED)
async def create(body: CompareIn, session: Session = Depends(get_session), user: User = Depends(current_user)) -> dict:
    try:  # fail fast on a bad request (unknown table / column / key) instead of queueing something doomed
        await run_in_threadpool(resolve, session, body)
    except JobError as e:
        raise _request_error(e) from e
    c = Comparison(name=body.name or f"{_label(body.source)} → {_label(body.target)}", spec=body.model_dump(mode="json"),
                   job_id=body.job_id, owner_id=user.id, phase="Queued")
    session.add(c)
    session.commit()
    _dispatch(c, session)
    session.refresh(c)
    return snapshot(c)


@router.get("", dependencies=[Depends(current_user)])
def list_comparisons(session: Session = Depends(get_session)) -> list[dict]:
    return [snapshot(c) for c in session.scalars(select(Comparison).order_by(Comparison.created_at.desc()).limit(50))]


@router.get("/{cid}", dependencies=[Depends(current_user)])
def get_comparison(cid: uuid.UUID, session: Session = Depends(get_session)) -> dict:
    return snapshot(_or_404(session, cid))


def _finished(c: Comparison) -> dict[str, Any]:
    if c.status != "succeeded" or not c.summary:
        raise HTTPException(409, f"This comparison has no results (it is {c.status})")
    return c.summary


@router.get("/{cid}/rows", dependencies=[Depends(current_user)])
async def result_rows(cid: uuid.UUID, set: str = Query("missing"), offset: int = Query(0, ge=0),  # noqa: A002
                      limit: int = Query(100, ge=1, le=1000), session: Session = Depends(get_session)) -> dict:
    c = _or_404(session, cid)
    summary = _finished(c)
    try:
        return await run_in_threadpool(cexport.rows, c.id, summary, set, offset, limit)
    except JobError as e:
        raise HTTPException(422, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status.HTTP_410_GONE, str(e)) from e


@router.get("/{cid}/export", dependencies=[Depends(current_user)])
async def export(cid: uuid.UUID, sets: str = Query(..., description="comma separated: missing,extra,mismatched,dup_source,dup_target"),
                 columns: str = Query("all", description="all | keys | comma separated column names"),
                 format: str = Query("csv"), rows: str = Query("all", pattern="^(all|sample)$"),  # noqa: A002
                 session: Session = Depends(get_session)) -> FileResponse:
    c = _or_404(session, cid)
    summary = _finished(c)
    cols: str | list[str] = columns if columns in ("all", "keys") else [x for x in columns.split(",") if x]
    sample_rows = int((summary.get("options") or {}).get("sample_rows", 100))
    try:
        path, filename, media = await run_in_threadpool(
            cexport.export, c.id, c.name, summary, [s for s in sets.split(",") if s], cols, format, rows == "sample", sample_rows)
    except JobError as e:
        raise HTTPException(422, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status.HTTP_410_GONE, str(e)) from e
    return FileResponse(path, media_type=media, filename=filename, background=BackgroundTask(cexport.remove_folder, path.parent))


@router.post("/{cid}/cancel", dependencies=[Depends(current_user)])
def cancel(cid: uuid.UUID, session: Session = Depends(get_session)) -> dict:
    c = _or_404(session, cid)
    if c.status in FINISHED:
        raise HTTPException(409, f"This comparison already {c.status}")
    if c.status == "queued" or is_stalled(c):  # nothing is running it: finish it here
        c.status, c.phase, c.finished_at = "cancelled", "Cancelled", utcnow()
        shutil.rmtree(folder(c.id), ignore_errors=True)
    else:
        c.cancel_requested = True
    session.commit()
    return snapshot(c)


@router.delete("/{cid}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(current_user)])
def delete(cid: uuid.UUID, session: Session = Depends(get_session)) -> None:
    c = _or_404(session, cid)
    if c.status not in FINISHED and not is_stalled(c):
        raise HTTPException(409, "Cancel the comparison before deleting it")
    shutil.rmtree(folder(c.id), ignore_errors=True)
    session.delete(c)
    session.commit()
