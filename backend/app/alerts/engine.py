"""Alert rules against what happens, keeping only current state (alert_states).

Events arrive two ways:
- hooks on existing code paths: a job ends (engine/runner.run_job), the scheduler launches or can't launch a
  pipeline (engine/schedule.py), a comparison ends (compare/engine.py);
- evaluate(), a beat task every 30 s, for states: connections, service checks, host resources, stalled jobs,
  stale remote inputs. Exactly one beat runs it; it is idempotent anyway (it only flips rows that change).

observe() is the one transition: a subject going bad fires once (after its threshold), staying bad sends nothing,
recovering sends once. While alerts are muted the state keeps moving but nothing is sent; when the mute ends one
summary per channel lists what is still firing (and what recovered after its break message went out).
Every hook swallows its own errors: an alert problem never fails or slows the job, check or scheduler.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_sessionmaker
from ..models import (AlertRule, AlertState, Comparison, Connection, Job, Pipeline, RemoteLoad, ServiceCheck,
                      Setting, utcnow)
from . import delivery
from .catalog import EVENTS
from .messages import Message, absolute, aware, clean, fmt_duration, fmt_time

log = logging.getLogger("conduit.alerts")

MUTE_KEY = "alerts.mute_until"
STALLED_S = 90  # the Jobs screen's rule: a running job silent this long shows as stalled
STATE_EVENTS = ("connection_down", "check_down", "host_high", "job_stalled", "input_stale")


@dataclass
class Subject:
    kind: str  # pipeline | job | connection | check | host | comparison
    id: str
    label: str
    link: str = ""  # an app page: "#/jobs/<id>"


class Outbox:
    """Messages to send once the transaction that decided them has committed."""

    def __init__(self) -> None:
        self.items: list[tuple[uuid.UUID, Message]] = []

    def add(self, rule: AlertRule, m: Message) -> None:
        self.items += [(ch.id, m) for ch in rule.channels if ch.enabled]


def _safe(fn: Callable, *args: Any) -> None:
    try:
        fn(*args)
    except Exception:  # noqa: BLE001 - alerts must never take down what triggered them
        log.exception("alert hook %s failed", fn.__name__)


# ---- mute -------------------------------------------------------------------------------------------------

def mute_until(s: Session) -> datetime | None:
    row = s.get(Setting, MUTE_KEY)
    return aware(datetime.fromisoformat(row.value)) if row and row.value else None


def is_muted(s: Session, now: datetime) -> bool:
    until = mute_until(s)
    return until is not None and until > now


# ---- messages ---------------------------------------------------------------------------------------------

def _title(rule: AlertRule, subj_label: str, kind: str) -> str:
    ev = rule.event
    if ev == "job_failed":
        return f"{'Pipeline' if kind == 'pipeline' else 'Job'} failed: {subj_label}"
    return {"job_stalled": f"Job stalled: {subj_label}", "schedule_failed": f"Schedule couldn't start: {subj_label}",
            "input_stale": f"No new input file: {subj_label}", "connection_down": f"Connection down: {subj_label}",
            "check_down": f"Service check down: {subj_label}", "host_high": f"{subj_label} high",
            "compare_diff": f"Compare found differences: {subj_label}"}.get(ev, f"{EVENTS[ev].label}: {subj_label}")


def _base(rule: AlertRule, st: AlertState | Subject, status: str) -> dict[str, Any]:
    kind = st.subject_kind if isinstance(st, AlertState) else st.kind
    sid = st.subject_id if isinstance(st, AlertState) else st.id
    label = st.subject_label if isinstance(st, AlertState) else st.label
    return {"status": status, "event": rule.event, "rule": {"id": str(rule.id), "name": rule.name},
            "subject": {"kind": kind, "id": sid, "name": label}, "link": absolute(st.link),
            "link_label": "Open the job" if "#/jobs/" in (st.link or "") else "Open in Conduit"}


def firing_message(rule: AlertRule, st: AlertState, note: str = "") -> Message:
    since = st.bad_since or st.fired_at
    lines = [f"{note} · since {fmt_time(since)}" if note else f"Since {fmt_time(since)}", f"Rule: {rule.name}"]
    return Message(title=_title(rule, st.subject_label, st.subject_kind), lines=lines, detail=st.reason or None,
                   since=aware(since).isoformat() if since else None, reason=st.reason or None, **_base(rule, st, "firing"))


def recovered_message(rule: AlertRule, st: AlertState, now: datetime) -> Message:
    since = aware(st.bad_since or st.fired_at or now)
    what = {"job_failed": "Ran fine again", "job_stalled": "Moving again (or finished)",
            "schedule_failed": "The schedule started it again", "input_stale": "A new file loaded",
            "connection_down": "Back up", "check_down": "Back up", "host_high": "Back below the threshold"}.get(rule.event, "Cleared")
    lines = [f"{what} · {'was failing' if rule.event in ('job_failed', 'schedule_failed') else 'lasted'} "
             f"{fmt_duration((now - since).total_seconds())} (since {fmt_time(since)})", f"Rule: {rule.name}"]
    return Message(title=f"Recovered: {st.subject_label}", lines=lines, since=since.isoformat(),
                   **_base(rule, st, "recovered"))


# ---- the one transition ------------------------------------------------------------------------------------

def _state(s: Session, rule: AlertRule, kind: str, sid: str) -> AlertState | None:
    return s.scalar(select(AlertState).where(AlertState.rule_id == rule.id, AlertState.subject_kind == kind,
                                             AlertState.subject_id == sid))


def observe(s: Session, rule: AlertRule, subj: Subject, *, bad: bool, now: datetime, muted: bool, out: Outbox,
            reason: str = "", since: datetime | None = None, for_at_least: timedelta = timedelta(0),
            hold: bool = False, note: str = "") -> None:
    """Move one (rule, subject) on. `bad`: the condition holds now; it fires once it has held `for_at_least`.
    `hold`: not bad by this rule's threshold, but not well either (a warn after err): a firing alert stays."""
    st = _state(s, rule, subj.kind, subj.id)
    if bad:
        if st is None:
            st = AlertState(rule_id=rule.id, subject_kind=subj.kind, subject_id=subj.id, bad_since=since or now,
                            firing=False, notified="none")
            s.add(st)
        elif st.bad_since is None:  # recovered while muted, and broke again
            st.bad_since = since or now
        st.subject_label, st.link, st.reason = subj.label[:300], subj.link[:300], clean(reason)
        if not st.firing and now - aware(st.bad_since) >= for_at_least:
            st.firing, st.fired_at = True, now
            if not muted:
                out.add(rule, firing_message(rule, st, note))
                st.notified = "firing"
        return
    if st is None or (hold and st.firing):
        return
    if st.notified == "firing":
        if muted:  # its break went out: the unmute summary says it recovered
            st.firing, st.bad_since = False, None
            return
        out.add(rule, recovered_message(rule, st, now))
    s.delete(st)


def _once(rule: AlertRule, subj: Subject, title_reason: str, lines: list[str], muted: bool, out: Outbox) -> None:
    """A one-shot event: one message, no state (nothing ever clears it)."""
    if muted:
        return
    out.add(rule, Message(title=_title(rule, subj.label, subj.kind), lines=lines + [f"Rule: {rule.name}"],
                          detail=title_reason or None, reason=title_reason or None, **_base(rule, subj, "firing")))


def _rules(s: Session, *events: str) -> list[AlertRule]:
    return list(s.scalars(select(AlertRule).where(AlertRule.enabled.is_(True), AlertRule.event.in_(events))))


def _pipeline_in_scope(rule: AlertRule, pipeline_id: Any) -> bool:
    scope = rule.scope or {}
    return bool(scope.get("all", not scope)) or (pipeline_id is not None and str(pipeline_id) in scope.get("pipelines", []))


# ---- hooks ------------------------------------------------------------------------------------------------

def job_finished(job_id: uuid.UUID | str, status: str) -> None:
    """engine/runner.run_job, after a job ends: failed fires job_failed, success clears it for that pipeline."""
    _safe(_job_finished, job_id, status)


def _job_finished(job_id: uuid.UUID | str, status: str) -> None:
    if status not in ("succeeded", "failed"):
        return
    out = Outbox()
    with get_sessionmaker()() as s:
        rules = _rules(s, "job_failed")
        job = s.get(Job, uuid.UUID(str(job_id))) if rules else None
        if job is None:
            return
        now, muted = utcnow(), is_muted(s, utcnow())
        if job.pipeline_id:
            p = s.get(Pipeline, job.pipeline_id)
            subj = Subject("pipeline", str(job.pipeline_id), p.name if p else job.name, f"#/jobs/{job.id}")
            note = "Scheduled run failed" if job.trigger == "schedule" else "Run failed"
            for r in rules:
                if status == "succeeded":  # any good run clears it: the pipeline works again
                    observe(s, r, subj, bad=False, now=now, muted=muted, out=out)
                elif _job_scope_ok(r, job):
                    observe(s, r, subj, bad=True, now=now, muted=muted, out=out, reason=job.error or "",
                            since=aware(job.finished_at) or now, note=note)
        elif status == "failed":
            subj = Subject("job", str(job.id), job.name, f"#/jobs/{job.id}")
            for r in rules:
                if (r.options or {}).get("jobs", "all") == "all" and (r.scope or {}).get("all", True):
                    _once(r, subj, clean(job.error), [f"Load failed at {fmt_time(job.finished_at or now)}"], muted, out)
        s.commit()
    delivery.enqueue(out.items)


def _job_scope_ok(rule: AlertRule, job: Job) -> bool:
    which = (rule.options or {}).get("jobs", "all")
    if which == "scheduled" and job.trigger != "schedule":
        return False
    return _pipeline_in_scope(rule, job.pipeline_id)


def schedule_result(pipeline_id: uuid.UUID | str, error: str | None) -> None:
    """engine/schedule.py: a due pipeline launched (error None) or couldn't start (why)."""
    _safe(_schedule_result, pipeline_id, error)


def _schedule_result(pipeline_id: uuid.UUID | str, error: str | None) -> None:
    out = Outbox()
    with get_sessionmaker()() as s:
        rules = _rules(s, "schedule_failed")
        p = s.get(Pipeline, uuid.UUID(str(pipeline_id))) if rules else None
        if p is None:
            return
        now, muted = utcnow(), is_muted(s, utcnow())
        subj = Subject("pipeline", str(p.id), p.name, f"#/pipelines/{p.id}")
        for r in rules:
            if error is None or _pipeline_in_scope(r, p.id):
                observe(s, r, subj, bad=error is not None, now=now, muted=muted, out=out, reason=error or "",
                        since=aware(p.next_run_at) or now, note="Due")
        s.commit()
    delivery.enqueue(out.items)


def comparison_finished(cid: uuid.UUID | str) -> None:
    """compare/engine.py, after a comparison succeeds: differences send one message per comparison."""
    _safe(_comparison_finished, cid)


def _comparison_finished(cid: uuid.UUID | str) -> None:
    out = Outbox()
    with get_sessionmaker()() as s:
        rules = _rules(s, "compare_diff")
        c = s.get(Comparison, uuid.UUID(str(cid))) if rules else None
        sm = (c.summary or {}) if c else {}
        counts = {"missing": sm.get("missing", 0), "extra": sm.get("extra", 0), "different": sm.get("mismatched", 0),
                  "duplicate": (sm.get("dup_source_rows", 0) or 0) + (sm.get("dup_target_rows", 0) or 0)}
        if c is None or not any(counts.values()):
            return
        line = " · ".join(f"{n:,} {k}" for k, n in counts.items() if n)
        subj = Subject("comparison", str(c.id), c.name, f"#/compare/{c.id}")
        muted = is_muted(s, utcnow())
        for r in rules:
            _once(r, subj, "", [f"{line} rows", f"Source {sm.get('rows_source', 0):,} rows · target {sm.get('rows_target', 0):,} rows"], muted, out)
    delivery.enqueue(out.items)


# ---- the 30 s evaluator ---------------------------------------------------------------------------------------

def evaluate(now: datetime | None = None, host_snapshot: Callable[[], dict] | None = None) -> int:
    """Check every state-based rule against the current rows; end a mute that ran out. Returns messages queued."""
    now = now or utcnow()
    out = Outbox()
    with get_sessionmaker()() as s:
        _end_mute(s, now, out)  # first: the summary covers what happened while muted, then normal sending resumes
        muted = is_muted(s, now)
        rules = list(s.scalars(select(AlertRule).where(AlertRule.enabled.is_(True))))
        seen: set[tuple[Any, str, str]] = set()
        by = {e: [r for r in rules if r.event == e] for e in EVENTS}
        ctx = dict(s=s, now=now, muted=muted, out=out, seen=seen)
        if by["connection_down"]:
            _connections(by["connection_down"], **ctx)
        if by["check_down"]:
            _checks(by["check_down"], **ctx)
        if by["host_high"]:
            _host(by["host_high"], host_snapshot, **ctx)
        if by["job_stalled"]:
            _stalled(by["job_stalled"], **ctx)
        if by["input_stale"]:
            _stale(by["input_stale"], **ctx)
        _cleanup(s, {r.id: r for r in rules}, seen)
        s.commit()
    delivery.enqueue(out.items)
    return len(out.items)


def _see(rule, subj, seen, **kw):
    seen.add((rule.id, subj.kind, subj.id))
    observe(rule=rule, subj=subj, **kw)


def _connections(rules, *, s, now, muted, out, seen) -> None:
    conns = list(s.scalars(select(Connection)))
    for r in rules:
        scope, opts = r.scope or {}, r.options or {}
        k, warn = int(opts.get("k", 2)), bool(opts.get("include_warn"))
        for c in conns:
            if not (scope.get("all", not scope) or str(c.id) in scope.get("connections", [])
                    or (c.env and c.env in scope.get("envs", []))):
                continue
            streak, since = (c.bad_streak, c.bad_since) if warn else (c.err_streak, c.err_since)
            _see(r, Subject("connection", str(c.id), c.name, "#/monitor"), seen, s=s, bad=streak >= k,
                 hold=c.status != "ok", now=now, muted=muted, out=out, reason=c.note or c.status,
                 since=aware(since), note=f"{streak} failed check{'' if streak == 1 else 's'} in a row")


def _checks(rules, *, s, now, muted, out, seen) -> None:
    checks = list(s.scalars(select(ServiceCheck).where(ServiceCheck.enabled.is_(True))))
    for r in rules:
        scope, k = r.scope or {}, int((r.options or {}).get("k", 2))
        for c in checks:
            if not (scope.get("all", not scope) or str(c.id) in scope.get("checks", [])):
                continue
            _see(r, Subject("check", str(c.id), c.name, "#/monitor"), seen, s=s, bad=(c.fail_streak or 0) >= k,
                 hold=c.status != "up", now=now, muted=muted, out=out, reason=c.message,
                 since=aware(c.fail_since), note=f"{c.fail_streak} failed run{'' if c.fail_streak == 1 else 's'} in a row")


def _host(rules, snapshot_fn, *, s, now, muted, out, seen) -> None:
    from ..monitor import host

    try:
        snap = (snapshot_fn or host.snapshot)()
    except Exception:  # noqa: BLE001 - psutil trouble: try again next tick, keeping what is firing
        log.exception("host metrics for alerts failed")
        ids = [r.id for r in rules]
        seen.update((st.rule_id, st.subject_kind, st.subject_id)
                    for st in s.scalars(select(AlertState).where(AlertState.rule_id.in_(ids))))
        return
    items = {"memory": [("memory", "Memory", snap["memory"]["percent"])], "cpu": [("cpu", "CPU", snap["cpu"]["percent"])],
             "disk": [(f"disk:{d['label']}", f"Disk {d['label']}", d["percent"]) for d in snap.get("disks", [])]}
    default_pct = get_settings().monitor_warn_pct
    for r in rules:
        opts = r.options or {}
        pct, minutes = opts.get("pct") or default_pct, int(opts.get("minutes", 5))
        for res in opts.get("resources") or ["disk", "memory", "cpu"]:
            for sid, label, value in items.get(res, []):
                _see(r, Subject("host", sid, label, "#/monitor"), seen, s=s, bad=value >= pct,
                     for_at_least=timedelta(minutes=minutes), now=now, muted=muted, out=out,
                     reason=f"{label} at {value:.0f}% (threshold {pct}%)", note=f"Above {pct}% for {minutes} min")


def _stalled(rules, *, s, now, muted, out, seen) -> None:
    jobs = list(s.scalars(select(Job).where(Job.status == "running")))
    for r in rules:
        for j in jobs:
            if not _pipeline_in_scope(r, j.pipeline_id):
                continue
            hb = aware(j.heartbeat_at)
            stalled = hb is not None and (now - hb).total_seconds() > STALLED_S
            _see(r, Subject("job", str(j.id), j.name, f"#/jobs/{j.id}"), seen, s=s, bad=stalled, now=now, muted=muted,
                 out=out, since=hb, reason="No progress: its worker probably stopped. Retry it from the job page.",
                 note=f"Silent since {fmt_time(hb)}")
        # jobs no longer running (finished, retried, deleted) recover, with a message when their break went out
        for st in list(s.scalars(select(AlertState).where(AlertState.rule_id == r.id))):
            if (r.id, st.subject_kind, st.subject_id) not in seen:
                seen.add((r.id, st.subject_kind, st.subject_id))
                observe(s, r, Subject(st.subject_kind, st.subject_id, st.subject_label, st.link), bad=False, now=now,
                        muted=muted, out=out)


def _stale(rules, *, s, now, muted, out, seen) -> None:
    from ..remote import _keys_of

    pipes = [p for p in s.scalars(select(Pipeline).where(Pipeline.enabled.is_(True)))
             if p.schedule and (p.source or {}).get("remote")]
    for r in rules:
        hours = float((r.options or {}).get("hours", 24))
        for p in pipes:
            keys = _keys_of({"source": p.source, "target": p.target})  # only "newest" / "all" with skip-loaded
            if keys is None or not _pipeline_in_scope(r, p.id):
                continue
            last = s.scalar(select(func.max(RemoteLoad.loaded_at)).where(RemoteLoad.source_key == keys[0],
                                                                          RemoteLoad.target_key == keys[1]))
            ref = aware(last) or aware(p.created_at)
            why = f"Last new file loaded {fmt_time(last)}" if last else "No file loaded yet"
            _see(r, Subject("pipeline", str(p.id), p.name, f"#/pipelines/{p.id}"), seen, s=s,
                 bad=now - ref > timedelta(hours=hours), now=now, muted=muted, out=out, since=ref,
                 reason=f"{why} (more than {hours:g} h ago)", note=f"No new file for {hours:g} h")


def _cleanup(s: Session, rules: dict[Any, AlertRule], seen: set) -> None:
    """Drop, without a message, states nothing keeps alive: a disabled rule, a subject that is gone or out of scope."""
    for st in list(s.scalars(select(AlertState))):
        r = rules.get(st.rule_id)
        if r is None:
            s.delete(st)
        elif r.event in STATE_EVENTS:
            if (r.id, st.subject_kind, st.subject_id) not in seen:
                s.delete(st)
        elif st.subject_kind == "pipeline":
            p = s.get(Pipeline, uuid.UUID(st.subject_id))
            if p is None or not _pipeline_in_scope(r, p.id):
                s.delete(st)


# ---- mute -----------------------------------------------------------------------------------------------------

def _end_mute(s: Session, now: datetime, out: Outbox) -> None:
    row = s.get(Setting, MUTE_KEY)
    if row is None or (row.value and aware(datetime.fromisoformat(row.value)) > now):
        return
    s.delete(row)
    summarize(s, out)


def summarize(s: Session, out: Outbox) -> None:
    """The unmute summary: per channel, what is still firing, and what recovered after its break went out."""
    per: dict[uuid.UUID, list[dict[str, Any]]] = {}
    rules = {r.id: r for r in s.scalars(select(AlertRule).where(AlertRule.enabled.is_(True)))}
    for st in list(s.scalars(select(AlertState).order_by(AlertState.bad_since))):
        r = rules.get(st.rule_id)
        if r is None:
            continue
        if st.firing:
            item = {"title": _title(r, st.subject_label, st.subject_kind), "since": fmt_time(st.bad_since), "status": "firing"}
            st.notified = "firing"
        elif st.notified == "firing":
            item = {"title": _title(r, st.subject_label, st.subject_kind), "since": "", "status": "recovered"}
            s.delete(st)
        else:
            continue
        for ch in r.channels:
            if ch.enabled:
                per.setdefault(ch.id, []).append(item)
    for cid, items in per.items():
        firing = sum(1 for i in items if i["status"] == "firing")
        recovered = len(items) - firing
        title = f"Alerts unmuted: {firing} still firing" + (f", {recovered} recovered meanwhile" if recovered else "")
        out.items.append((cid, Message(status="summary", title=title, items=items, link=absolute("#/monitor"),
                                       link_label="Open Monitor")))
