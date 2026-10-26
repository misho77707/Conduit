"""The alert events: what each watches, which subjects a rule may pick, and its thresholds (validated on save)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Event:
    key: str
    label: str  # "Job failed"
    subject: str  # pipeline | job | connection | check | host | comparison
    scope_keys: tuple[str, ...] = ()  # what a rule may pick instead of "everything": pipelines, connections, envs, checks
    options: dict[str, Any] = field(default_factory=dict)  # option -> default
    oneshot: bool = False  # sends one message, keeps no firing state (nothing ever clears it)


EVENTS: dict[str, Event] = {e.key: e for e in (
    Event("job_failed", "Job failed", "pipeline", ("pipelines",), {"jobs": "all"}),
    Event("job_stalled", "Job stalled", "job", ("pipelines",)),
    Event("schedule_failed", "Schedule couldn't start", "pipeline", ("pipelines",)),
    Event("input_stale", "Pipeline input stale", "pipeline", ("pipelines",), {"hours": 24}),
    Event("connection_down", "Connection down", "connection", ("connections", "envs"), {"k": 2, "include_warn": False}),
    Event("check_down", "Service check down", "check", ("checks",), {"k": 2}),
    Event("host_high", "Host resource high", "host", (), {"resources": ["disk", "memory", "cpu"], "pct": None, "minutes": 5}),
    Event("compare_diff", "Compare found differences", "comparison", (), oneshot=True),
)}

RESOURCES = ("disk", "memory", "cpu")
JOB_SCOPES = ("scheduled", "pipelines", "all")


def _num(name: str, v: Any, lo: float, hi: float, integer: bool) -> float | int:
    try:
        n = int(v) if integer else float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if integer and float(v) != n:
        raise ValueError(f"{name} must be a whole number")
    if not lo <= n <= hi:
        raise ValueError(f"{name} must be between {lo:g} and {hi:g}")
    return n


def normalize_options(event: str, options: dict[str, Any] | None) -> dict[str, Any]:
    """The event's options with defaults filled in; ValueError with a readable message when one is out of bounds."""
    ev = EVENTS[event]
    opts = {**ev.options, **{k: v for k, v in (options or {}).items() if k in ev.options}}
    if event == "job_failed" and opts["jobs"] not in JOB_SCOPES:
        raise ValueError("jobs must be one of: scheduled, pipelines, all")
    if event == "input_stale":
        opts["hours"] = _num("hours", opts["hours"], 0.5, 24 * 30, False)
    if event in ("connection_down", "check_down"):
        opts["k"] = _num("k (consecutive checks)", opts["k"], 1, 20, True)
    if event == "connection_down":
        opts["include_warn"] = bool(opts["include_warn"])
    if event == "host_high":
        res = opts["resources"]
        if not isinstance(res, list) or not res or any(r not in RESOURCES for r in res):
            raise ValueError("resources must be one or more of: disk, memory, cpu")
        opts["resources"] = [r for r in RESOURCES if r in res]
        opts["pct"] = None if opts["pct"] in (None, "") else _num("pct", opts["pct"], 1, 100, True)
        opts["minutes"] = _num("minutes", opts["minutes"], 0, 24 * 60, True)
    return opts


def normalize_scope(event: str, scope: dict[str, Any] | None) -> dict[str, Any]:
    """{"all": true} or the picked subjects. Lists the event doesn't use are dropped; picking nothing means all."""
    ev = EVENTS[event]
    picked = {k: sorted({str(x) for x in (scope or {}).get(k) or []}) for k in ev.scope_keys}
    picked = {k: v for k, v in picked.items() if v}
    if not picked or (scope or {}).get("all"):
        return {"all": True}
    return picked
