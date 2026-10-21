import uuid
from typing import Any

import pyarrow as pa
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db import get_session
from ..deps import admin_only, viewer_ok
from ..engine import joblog
from ..engine.arrowconv import _jsonable
from ..engine.model import ReadRequest
from ..engine import combined
from ..engine.runner import JobError, prepare, short_error, snapshot
from ..engine.schedule import ScheduleError, next_after, validate_cron
from ..engine.sources import SourceUnavailable, raw_columns, source_adapter
from ..engine.spec import JobIn, OptionsIn, SourceIn, StepIn, StepOutCol, TargetIn
from ..engine.steps import StepError, apply_steps, compile_steps, infer_colspecs
from ..engine.types import to_arrow
from ..connectors import category_of
from ..models import Connection, Job, Pipeline, User, Watermark, utcnow
from ..schemas import PipelineIn, PipelineLastRunOut, PipelineOut, PipelinePatch, PipelinePreviewIn, PipelinePreviewOut
from .jobs import _dispatch

router = APIRouter(prefix="/pipelines", tags=["pipelines"])


def _pipeline_or_404(session: Session, pipeline_id: uuid.UUID) -> Pipeline:
    p = session.get(Pipeline, pipeline_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pipeline not found")
    return p


def _out(p: Pipeline, last_job: Job | None) -> PipelineOut:
    last_run = None
    if last_job is not None:
        last_run = PipelineLastRunOut(job_id=last_job.id, status=last_job.status, rows_written=last_job.rows_written,
                                      started_at=last_job.started_at, finished_at=last_job.finished_at, error=last_job.error)
    return PipelineOut(id=p.id, name=p.name, description=p.description, source=p.source, target=p.target,
                       options=p.options, steps=p.steps, output_columns=p.output_columns, source_columns=p.source_columns,
                       schedule=p.schedule,
                       enabled=p.enabled, next_run_at=p.next_run_at, created_at=p.created_at, updated_at=p.updated_at,
                       last_run=last_run, last_checked_at=p.last_checked_at, last_check_note=p.last_check_note)


def _next_run_for(schedule: str | None, enabled: bool):
    """None if there is nothing to schedule (no cron, or disabled); otherwise the next time it fires after now."""
    if not schedule or not enabled:
        return None
    try:
        return next_after(schedule, utcnow())
    except ScheduleError as e:
        raise HTTPException(422, str(e)) from e


def _check_schedule(schedule: str | None) -> None:
    if schedule is not None:
        try:
            validate_cron(schedule)
        except ScheduleError as e:
            raise HTTPException(422, str(e)) from e


@router.get("", dependencies=[Depends(viewer_ok)])
def list_pipelines(session: Session = Depends(get_session)) -> list[PipelineOut]:
    pipes = list(session.scalars(select(Pipeline).order_by(Pipeline.created_at)))
    job_ids = [p.last_job_id for p in pipes if p.last_job_id]
    jobs = {j.id: j for j in session.scalars(select(Job).where(Job.id.in_(job_ids)))} if job_ids else {}
    return [_out(p, jobs.get(p.last_job_id)) for p in pipes]


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(admin_only)])
def create_pipeline(body: PipelineIn, session: Session = Depends(get_session)) -> PipelineOut:
    _validate_shape(body.source, body.target, body.options, body.steps)
    _check_connections(session, body.source, body.target)
    _check_schedule(body.schedule)
    p = Pipeline(name=body.name, description=body.description, source=body.source, target=body.target,
                options=body.options, steps=body.steps, schedule=body.schedule, enabled=body.enabled,
                next_run_at=_next_run_for(body.schedule, body.enabled))
    session.add(p)
    try:
        session.commit()
    except Exception as e:  # noqa: BLE001 - most likely a duplicate name
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"Could not save the pipeline: {short_error(e)}") from e
    return _out(p, None)


@router.get("/{pipeline_id}", dependencies=[Depends(admin_only)])
def get_pipeline(pipeline_id: uuid.UUID, session: Session = Depends(get_session)) -> PipelineOut:
    p = _pipeline_or_404(session, pipeline_id)
    last_job = session.get(Job, p.last_job_id) if p.last_job_id else None
    return _out(p, last_job)


@router.patch("/{pipeline_id}", dependencies=[Depends(admin_only)])
def update_pipeline(pipeline_id: uuid.UUID, body: PipelinePatch, session: Session = Depends(get_session)) -> PipelineOut:
    p = _pipeline_or_404(session, pipeline_id)
    data = body.model_dump(exclude_unset=True)
    schema_affecting = {"source", "target", "options", "steps"} & data.keys()
    _validate_shape(data.get("source", p.source), data.get("target", p.target), data.get("options", p.options),
                    data.get("steps", p.steps))
    _check_connections(session, data.get("source", p.source), data.get("target", p.target))
    if "schedule" in data:
        _check_schedule(data["schedule"])
    for k, v in data.items():
        setattr(p, k, v)
    if schema_affecting:
        p.output_columns = None  # the cached schema no longer matches; /run needs a fresh preview first
        p.source_columns = None
    if "schedule" in data or "enabled" in data:
        p.next_run_at = _next_run_for(p.schedule, p.enabled)
    try:
        session.commit()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"Could not save the pipeline: {short_error(e)}") from e
    last_job = session.get(Job, p.last_job_id) if p.last_job_id else None
    return _out(p, last_job)


@router.delete("/{pipeline_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(admin_only)])
def delete_pipeline(pipeline_id: uuid.UUID, session: Session = Depends(get_session)) -> None:
    p = _pipeline_or_404(session, pipeline_id)
    # a combined source's watermarks are keyed by the pipeline ("pipeline:<id>:<driver>"): they go with it
    session.execute(delete(Watermark).where(Watermark.source_key.startswith(f"pipeline:{p.id}:")))
    session.delete(p)
    session.commit()


def _validate_shape(source: dict, target: dict, options: dict, steps: list) -> None:
    """Cheap, offline validation (no network) so a typo shows up before the first preview, not after."""
    try:
        s, o = SourceIn(**source), OptionsIn(**options)
        TargetIn(**target)
        step_models = [StepIn(**st) for st in steps]
    except Exception as e:  # noqa: BLE001 - a pydantic ValidationError, reworded for the API caller
        raise HTTPException(422, f"Invalid pipeline: {short_error(e)}") from e
    if steps and o.split_column:
        raise HTTPException(422, "Pipelines read the whole source in one piece; a split column is not supported yet")
    if steps and s.is_file and o.strategy != "full":
        raise HTTPException(422, "Files are always loaded whole: use the 'full' strategy")
    if s.combined is not None:
        if o.strategy != "full" or o.split_column:
            raise HTTPException(422, "A combined source is read as a whole: use the 'full' strategy without a split column "
                                     "(an incremental run is the driver input's watermark)")
        try:
            combined.check(s.combined)
        except combined.CombinedError as e:
            raise HTTPException(422, str(e)) from e
    try:
        compile_steps(step_models)
    except StepError as e:
        raise HTTPException(422, str(e)) from e


def _check_connections(session: Session, source: dict, target: dict) -> None:
    """A table source and the target must be databases, not file storage (S3 / SFTP / HTTP)."""
    sides = [("source", source), ("target", target)]
    if source.get("combined"):  # every table / query input, not the combined source itself
        sides = [(f"input {i.get('alias')}", i.get("source") or {}) for i in source["combined"].get("inputs") or []] + sides[1:]
    for role, side in sides:
        cid = side.get("connection_id")
        if role != "target" and (side.get("file") or side.get("remote")):
            continue
        conn = session.get(Connection, uuid.UUID(str(cid))) if cid else None
        if conn is not None and category_of(conn.type) != "database":
            raise HTTPException(422, f"{conn.name} is a file storage connection ({conn.type}): it cannot be the {role} "
                                     f"{'table' if role == 'source' else 'of a load'}")


# ---- preview ---------------------------------------------------------------------

def _rows_json(table: pa.Table, n: int) -> list[dict[str, Any]]:
    return [{k: (None if v is None else _jsonable(v) if not isinstance(v, (int, float, str, bool)) else v) for k, v in r.items()}
            for r in table.slice(0, n).to_pylist()]


def _preview(session: Session, p: Pipeline, sample_rows: int, upto_step: str | None) -> dict[str, Any]:
    s = SourceIn(**p.source)
    extra: dict[str, Any] = {}
    if s.combined is not None:  # inputs staged with a row cap, the join SQL run over the sample
        try:
            cp = combined.preview(session, s.combined, sample_rows)
        except combined.CombinedError as e:
            raise HTTPException(422, str(e)) from e
        except SourceUnavailable as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e
        extra = {"combined": cp.info, "source_columns": [c.to_dict() for c in cp.columns]}
        return _apply_steps_preview(p, cp.table, sample_rows, upto_step, extra)
    try:
        src = source_adapter(session, s)
        raw_cols = raw_columns(src, s)
    except SourceUnavailable as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e
    except JobError as e:
        raise HTTPException(422, str(e)) from e
    req = ReadRequest(table=s.table, query=s.query, where=s.where, columns=raw_cols, mode="full")
    try:
        splits = src.plan_splits(req, 1)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, short_error(e)) from e
    first = None
    if splits:
        gen = src.read_batches(req, splits[0], sample_rows)
        try:
            for b in gen:
                first = b
                break
        except Exception as e:  # noqa: BLE001 - driver / network error while sampling
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, short_error(e)) from e
        finally:
            gen.close()
    if first is not None:
        table = first.table
    else:  # nothing to sample (empty table / no matching rows): still infer the shape from the raw schema
        table = pa.Table.from_arrays([pa.array([], type=to_arrow(c.logical)) for c in raw_cols], names=[c.name for c in raw_cols])
    return _apply_steps_preview(p, table, sample_rows, upto_step, extra)


def _apply_steps_preview(p: Pipeline, table: pa.Table, sample_rows: int, upto_step: str | None,
                         extra: dict[str, Any]) -> dict[str, Any]:
    steps = compile_steps([StepIn(**sd) for sd in p.steps])
    if upto_step is not None:
        ids = [st.id for st in steps]
        if upto_step not in ids:
            raise HTTPException(422, f"No step with id {upto_step!r}")
        steps = steps[: ids.index(upto_step) + 1]
    # A broken (often just half-filled) step does not fail the preview: the result stops at the output of the
    # last step that worked and names the step that did not, so the builder can keep showing the table.
    error = error_step = None
    for st in steps:
        try:
            table = apply_steps([st], table)
        except StepError as e:
            error, error_step = str(e), st.id
            break
    cols = infer_colspecs(table)
    full_chain = upto_step is None and error is None  # only a complete, clean run may refresh the cached schema
    return {"columns": [c.to_dict() for c in cols], "rows": _rows_json(table, sample_rows),
            "sampled_rows": table.num_rows, "full_chain": full_chain, "error": error, "error_step": error_step, **extra}


@router.post("/{pipeline_id}/preview", dependencies=[Depends(admin_only)])
async def preview_pipeline(pipeline_id: uuid.UUID, body: PipelinePreviewIn,
                           session: Session = Depends(get_session)) -> PipelinePreviewOut:
    p = _pipeline_or_404(session, pipeline_id)
    result = await run_in_threadpool(_preview, session, p, body.sample_rows, body.upto_step)
    if result["full_chain"]:
        p.output_columns = result["columns"]
        p.source_columns = result.get("source_columns")  # a combined source's join schema (None otherwise)
        session.commit()
    return PipelinePreviewOut(**result)


# ---- run ---------------------------------------------------------------------

def _job_in(p: Pipeline) -> JobIn:
    return JobIn(
        name=p.name, source=SourceIn(**p.source), target=TargetIn(**p.target), columns=None,
        options=OptionsIn(**p.options), steps=[StepIn(**s) for s in p.steps],
        step_output_columns=[StepOutCol(**c) for c in p.output_columns] if p.output_columns else None,
        source_columns=[StepOutCol(**c) for c in p.source_columns] if p.source_columns else None,
    )


def _source_conn(s: SourceIn) -> uuid.UUID | None:
    """The job's source connection: a combined source's driver input (else its first database input)."""
    if s.combined is None:
        return s.connection_id
    ordered = sorted(s.combined.inputs, key=lambda i: i.alias != s.combined.driver)
    return next((i.source.connection_id for i in ordered if i.source.connection_id), None)


def _launch(session: Session, p: Pipeline, user: User) -> Job:
    """Build and dispatch a Job for this pipeline. Raises JobError/StepError/ValueError-family exceptions on a
    bad pipeline (no HTTP awareness), so app.engine.schedule's beat-driven runs can call this too. `_dispatch`
    itself can still raise HTTPException (broker down) - that propagates unchanged to whichever caller."""
    if not p.output_columns or ((p.source or {}).get("combined") and not p.source_columns):
        raise JobError("Refresh the preview before running this pipeline (no cached output schema yet)")
    body = _job_in(p)
    prepare(body, session)  # catch a bad table, column or step now instead of after queueing
    job = Job(name=p.name, kind="pipeline", spec=body.model_dump(mode="json"), source_conn_id=_source_conn(body.source),
             target_conn_id=body.target.connection_id, workers=body.options.workers, owner_id=user.id, pipeline_id=p.id)
    session.add(job)
    session.commit()
    joblog.push(job.id, "info", f"queued (pipeline {p.name!r})")
    _dispatch(job, session)
    p.last_job_id = job.id
    session.commit()
    session.refresh(job)
    return job


def _run(session: Session, p: Pipeline, user: User) -> dict[str, Any]:
    try:
        job = _launch(session, p, user)
    except (JobError, StepError) as e:
        raise HTTPException(422, str(e)) from e
    except (ValueError, FileNotFoundError, NotImplementedError) as e:
        raise HTTPException(422, str(e)) from e
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, short_error(e)) from e
    return snapshot(job)


@router.post("/{pipeline_id}/run", dependencies=[Depends(admin_only)])
def run_pipeline(pipeline_id: uuid.UUID, session: Session = Depends(get_session), user: User = Depends(admin_only)) -> dict:
    p = _pipeline_or_404(session, pipeline_id)
    return _run(session, p, user)
