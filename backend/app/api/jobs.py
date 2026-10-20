import asyncio
import json
import threading
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session, get_sessionmaker
from ..deps import current_user, current_user_sse
from ..engine import joblog
from ..engine.runner import JobError, make_plan, prepare, rejects_path, run_job, short_error, snapshot
from ..engine.spec import JobIn
from ..connectors.base import UserFacingError
from ..engine.steps import StepError
from ..remote import display as remote_display
from ..remote import remove_job_dir
from ..engine.combined_run import remove_job_dir as remove_combined_dir
from ..models import Job, JobSplit, User, utcnow

router = APIRouter(prefix="/jobs", tags=["jobs"])
_ACTIVE = ("queued", "planning", "running")


def _job_or_404(session: Session, job_id: uuid.UUID) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return job


def _stalled(job: Job) -> bool:
    """Marked running but silent: its worker died and the queue has not redelivered the task."""
    return snapshot(job, [])["status"] == "stalled"


def _json(obj: Any) -> str:
    return json.dumps(obj, default=str)


def _plan_or_422(spec: JobIn, session: Session, sample_rows: int) -> dict[str, Any]:
    try:
        return make_plan(prepare(spec, session), sample_rows=sample_rows)
    except (JobError, StepError, UserFacingError) as e:  # UserFacingError: remote files (no match, too big, ...)
        raise HTTPException(422, str(e)) from e
    except (ValueError, FileNotFoundError, NotImplementedError) as e:
        raise HTTPException(422, str(e)) from e
    except Exception as e:  # noqa: BLE001 - driver / network errors while probing source or target
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, short_error(e)) from e


@router.post("/plan", dependencies=[Depends(current_user)])
async def plan(body: JobIn, sample_rows: int = Query(2000, ge=0, le=20000), session: Session = Depends(get_session)) -> dict:
    """Dry run for the review step: columns, DDL, splits, row estimate and a timed sample. Writes nothing."""
    return await run_in_threadpool(_plan_or_422, body, session, sample_rows)


def _dispatch(job: Job, session: Session) -> None:
    if get_settings().job_runner == "thread":
        threading.Thread(target=run_job, args=(job.id,), daemon=True, name=f"job-{str(job.id)[:8]}").start()
        return
    from ..worker.tasks import run_job_task

    try:
        res = run_job_task.apply_async(args=[str(job.id)], retry=False)
        job.celery_id = res.id
        session.commit()
    except Exception as e:  # noqa: BLE001 - broker down
        job.status, job.error, job.finished_at = "failed", f"Could not queue the job: {short_error(e)}", utcnow()
        session.commit()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The job queue (Redis) is not reachable") from e


def _create(body: JobIn, session: Session, user: User) -> Job:
    try:
        prepare(body, session)  # catch a bad table, column or option now instead of after queueing
    except (JobError, StepError, UserFacingError) as e:
        raise HTTPException(422, str(e)) from e
    except (ValueError, FileNotFoundError, NotImplementedError) as e:
        raise HTTPException(422, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, short_error(e)) from e
    if body.source.remote:
        src = remote_display(body.source.remote, session)  # s3://bucket/exports/orders_*.csv; never a query string
    else:
        src = body.source.file.path if body.source.file else (body.source.table or "query")
    job = Job(
        name=body.name or f"{src} → {body.target.table}", kind="file_load" if body.source.is_file else "transfer",
        spec=body.model_dump(mode="json"), source_conn_id=body.source.connection_id, target_conn_id=body.target.connection_id,
        workers=body.options.workers, owner_id=user.id,
    )
    session.add(job)
    session.commit()
    return job


@router.post("", status_code=status.HTTP_201_CREATED)
def create_job(body: JobIn, session: Session = Depends(get_session), user: User = Depends(current_user)) -> dict:
    job = _create(body, session, user)
    joblog.push(job.id, "info", "queued")
    _dispatch(job, session)
    session.refresh(job)
    return snapshot(job)


@router.get("", dependencies=[Depends(current_user)])
def list_jobs(session: Session = Depends(get_session)) -> list[dict]:
    jobs = list(session.scalars(select(Job).order_by(Job.created_at.desc()).limit(50)))
    order = {"running": 0, "planning": 0, "queued": 1}
    jobs.sort(key=lambda j: order.get(j.status, 2))
    return [snapshot(j, []) if j.status not in _ACTIVE else snapshot(j) for j in jobs]


def _detail(session: Session, job: Job, log_lines: int = 40) -> dict[str, Any]:
    splits = list(session.scalars(select(JobSplit).where(JobSplit.job_id == job.id).order_by(JobSplit.idx)))
    snap = snapshot(job, splits)
    snap["splits"] = [{"idx": s.idx, "label": s.label, "status": s.status, "est_rows": s.est_rows, "rows_written": s.rows_written,
                       "rows_rejected": s.rows_rejected, "attempts": s.attempts, "error": s.error} for s in splits]
    snap["plan"] = job.plan
    snap["spec"] = job.spec  # the original request, so the wizard can reopen it for editing
    snap["log"] = joblog.tail(job.id, log_lines)
    return snap


@router.get("/{job_id}", dependencies=[Depends(current_user)])
def get_job(job_id: uuid.UUID, session: Session = Depends(get_session)) -> dict:
    return _detail(session, _job_or_404(session, job_id))


@router.get("/{job_id}/log", dependencies=[Depends(current_user)])
def get_log(job_id: uuid.UUID, n: int = Query(100, ge=1, le=200), session: Session = Depends(get_session)) -> list[dict]:
    _job_or_404(session, job_id)
    return joblog.tail(job_id, n)


@router.post("/{job_id}/cancel", dependencies=[Depends(current_user)])
def cancel_job(job_id: uuid.UUID, session: Session = Depends(get_session)) -> dict:
    job = _job_or_404(session, job_id)
    if job.status not in _ACTIVE:
        raise HTTPException(409, f"Job is already {job.status}")
    job.cancel_requested = True
    if job.status == "queued" or _stalled(job):  # nothing is running it (never picked up, or its worker died): finish it here
        job.status, job.finished_at = "cancelled", utcnow()
    session.commit()
    joblog.push(job.id, "warn", "cancel requested")
    return snapshot(job, [])


@router.post("/{job_id}/retry", dependencies=[Depends(current_user)])
def retry_job(job_id: uuid.UUID, session: Session = Depends(get_session)) -> dict:
    """Continue a failed or cancelled job: finished splits stay done, the rest resume from their last committed batch."""
    job = _job_or_404(session, job_id)
    if job.status not in ("failed", "cancelled") and not _stalled(job):
        raise HTTPException(409, f"Only failed, cancelled or stalled jobs can be retried (this one is {job.status})")
    job.status, job.cancel_requested, job.error, job.finished_at = "queued", False, None, None
    session.commit()
    joblog.push(job.id, "info", "retry queued")
    _dispatch(job, session)
    session.refresh(job)
    return snapshot(job)


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(current_user)])
def delete_job(job_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    job = _job_or_404(session, job_id)
    if job.status in _ACTIVE:
        raise HTTPException(409, "Cancel the job before deleting it")
    rejects_path(job.id).unlink(missing_ok=True)
    remove_job_dir(job.id)
    remove_combined_dir(job.id)
    joblog.clear(job.id)
    session.delete(job)
    session.commit()


@router.get("/{job_id}/rejects", dependencies=[Depends(current_user)])
def download_rejects(job_id: uuid.UUID, session: Session = Depends(get_session)) -> FileResponse:
    _job_or_404(session, job_id)
    p = rejects_path(job_id)
    if not p.exists() or p.stat().st_size == 0:
        raise HTTPException(404, "This job has no rejected rows")
    return FileResponse(p, media_type="text/csv", filename=f"{job_id}.rejects.csv")


def _read_detail(job_id: uuid.UUID) -> dict[str, Any] | None:
    with get_sessionmaker()() as s:
        job = s.get(Job, job_id)
        return _detail(s, job, 15) if job else None


@router.get("/{job_id}/events", dependencies=[Depends(current_user_sse)])
async def events(job_id: uuid.UUID) -> StreamingResponse:
    """Server-sent events: the job's state once a second until it finishes. Pass ?token=<jwt> (EventSource cannot set headers)."""
    first = await run_in_threadpool(_read_detail, job_id)
    if first is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")

    async def stream():
        detail = first
        while True:
            yield f"event: job\ndata: {_json(detail)}\n\n"
            if detail["status"] not in (*_ACTIVE, "stalled"):
                yield "event: done\ndata: {}\n\n"
                return
            await asyncio.sleep(1.0)
            detail = await run_in_threadpool(_read_detail, job_id) or detail

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
