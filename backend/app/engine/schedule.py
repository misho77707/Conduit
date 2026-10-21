"""Pipeline scheduling: which pipelines are due, and running them. Only the next fire time is kept (no history).

Mirrors app/monitor/checks.py's optimistic-claim pattern (there: ServiceCheck.claimed_at / interval_s; here:
Pipeline.schedule_claimed_at / next_run_at computed from a cron expression via croniter). A pipeline only ever
becomes due if it is enabled, has a schedule, AND already has a cached preview schema (Pipeline.output_columns) -
the same guard /run enforces for a manual run. Saving a schedule never triggers an immediate run (unlike a
Monitor check): a run this instant might write to a real target, so a schedule only ever fires on its own clock.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from croniter import CroniterBadCronError, CroniterBadDateError, CroniterNotAlphaError, croniter
from sqlalchemy import case, select, update
from sqlalchemy.orm import Session

from ..db import get_sessionmaker
from ..models import Pipeline, User, utcnow

log = logging.getLogger("conduit.scheduler")

STALE_CLAIM_S = 60  # a scheduled run that claimed a pipeline and never finished (killed worker) is retried after this
_CRON_ERRORS = (CroniterBadCronError, CroniterBadDateError, CroniterNotAlphaError, ValueError)


class ScheduleError(Exception):
    """The schedule string is not something croniter can parse."""


def validate_cron(expr: str) -> None:
    """Raise ScheduleError unless `expr` is a cron croniter understands (5 fields, or a shortcut like '@daily')."""
    if not croniter.is_valid((expr or "").strip()):
        raise ScheduleError(f"{expr!r} is not a cron expression (5 fields, e.g. '0 2 * * *') or a shortcut like '@daily'")


def next_after(expr: str, after: datetime) -> datetime:
    """The next time this cron expression fires, strictly after `after`."""
    try:
        return croniter(expr.strip(), after).get_next(datetime)
    except _CRON_ERRORS as e:
        raise ScheduleError(str(e)) from e


def _aware(d):
    return d if d is None or d.tzinfo else d.replace(tzinfo=utcnow().tzinfo)


def is_due(p: Pipeline, now: datetime | None = None) -> bool:
    now = now or utcnow()
    if not (p.enabled and p.schedule and p.output_columns):
        return False  # disabled, no schedule, or no cached preview to run against yet
    claimed = _aware(p.schedule_claimed_at)
    if claimed and (now - claimed).total_seconds() < STALE_CLAIM_S:
        return False  # another beat tick (or worker) is running it right now
    next_run = _aware(p.next_run_at)
    return next_run is not None and next_run <= now


def _claim(session: Session, pipeline_id) -> bool:
    """Atomically mark a pipeline as being run, so overlapping beat ticks never queue it twice."""
    now = utcnow()
    stale = now - timedelta(seconds=STALE_CLAIM_S)
    res = session.execute(update(Pipeline).where(
        Pipeline.id == pipeline_id, (Pipeline.schedule_claimed_at.is_(None)) | (Pipeline.schedule_claimed_at < stale))
        .values(schedule_claimed_at=now))
    session.commit()
    return res.rowcount == 1


def _run_as(session: Session, p: Pipeline) -> User | None:
    """Who a scheduled run is recorded as (the job shows "Started by: schedule" either way): the pipeline's owner while
    they are an active admin or superadmin, else the longest-standing active superadmin, then admin. Never a viewer
    or a deactivated account, so deactivating someone never stops the schedules."""
    owner = session.get(User, p.owner_id) if p.owner_id else None
    if owner is not None and owner.is_active and owner.role in ("admin", "superadmin"):
        return owner
    q = (select(User).where(User.is_active.is_(True), User.role.in_(("superadmin", "admin")))
         .order_by(case((User.role == "superadmin", 0), else_=1), User.created_at))
    return session.scalars(q).first()


def run_and_dispatch(pipeline_id) -> bool:
    """Queue a run for one due pipeline (skipped when another tick already holds it). True if a job was queued."""
    from ..api.pipelines import _launch  # local import: avoids a module-load cycle with app.api.pipelines

    Session = get_sessionmaker()
    with Session() as s:
        if not _claim(s, pipeline_id):
            return False
        p = s.get(Pipeline, pipeline_id)
        if p is None:
            return False
        queued = False
        user = _run_as(s, p)
        go, note = _worth_running(s, p)
        if not go:  # a remote source with nothing new: no job (it would only push real loads out of the Jobs list)
            p.last_checked_at, p.last_check_note = utcnow(), note[:500]
        elif user is None:
            log.error("pipeline %s is due but there is no active admin or superadmin to run it as", pipeline_id)
        else:
            try:
                _launch(s, p, user, trigger="schedule")
                queued = True
                if note:
                    p.last_checked_at, p.last_check_note = utcnow(), f"load started: {note}"[:500]
            except Exception:  # noqa: BLE001 - a broken pipeline must never take the scheduler down
                log.exception("scheduled run of pipeline %s (%r) failed to queue", pipeline_id, p.name)
        # Always advance next_run_at, even on failure: a broken schedule waits for its next slot instead of
        # retrying every beat tick.
        try:
            p.next_run_at = next_after(p.schedule, utcnow())
        except ScheduleError:
            p.next_run_at = None  # the stored cron somehow went bad; stop firing until it's edited again
        p.schedule_claimed_at = None
        s.commit()
    return queued


def _worth_running(session: Session, p: Pipeline) -> tuple[bool, str]:
    """Does a scheduled run have anything to do? Only a remote "newest" / "all" source with skip-loaded can say no:
    the match is resolved first, and nothing new (or another load from it still running) means no job."""
    if not (p.source or {}).get("remote"):
        return True, ""
    from ..remote import has_new_files

    try:
        return has_new_files(session, {"source": p.source, "target": p.target})
    except Exception as e:  # noqa: BLE001 - e.g. the server is down: say so on the pipeline, try again next time
        from .runner import short_error

        return False, f"check failed: {short_error(e)}"


def run_due(max_workers: int = 4) -> int:
    """Queue a run for every enabled, previewed pipeline whose schedule has come due. Called by beat."""
    with get_sessionmaker()() as s:
        due = [p.id for p in s.scalars(select(Pipeline)) if is_due(p)]
    if not due:
        return 0
    with ThreadPoolExecutor(max_workers=min(max_workers, len(due)), thread_name_prefix="conduit-sched") as pool:
        return sum(1 for ran in pool.map(run_and_dispatch, due) if ran)
