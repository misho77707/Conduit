"""Which of a database's driver options moves a job side's data (connectors/base.DriverOption).

Requested: the job's `source_driver` / `target_driver`, else the connection's `driver`, else the classic option.
- "auto": the first (fastest) option serving the side whose checks pass; otherwise the classic one, with a note
  saying exactly why ("source: using psycopg — column `tags` is `text[]` ...").
- an explicit option that fails a check refuses the job with the reason: it never falls back.
Checks run before anything is written: is the package importable, and `adapter.driver_problem()` (does the fast
path handle every column type, is the server setting it needs on). Catalog work (columns, counts, splits, DDL)
always runs on the classic driver; only reading and writing batches switch.

Mid-run, a fast driver that meets something it can't do raises CapabilityError; the runner may retry that split
on the classic driver (auto only, and only when the retry cannot duplicate rows).
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from ..connectors.base import UserFacingError

AUTO = "auto"


class DriverRefused(UserFacingError, ValueError):
    """An explicitly chosen driver cannot run this job side (the reason is for people)."""


class CapabilityError(Exception):
    """A fast driver met a value / type / server setting it cannot handle while running."""

    def __init__(self, side: str, message: str):
        super().__init__(message)
        self.side = side  # source | target


@dataclass
class Choice:
    side: str  # source | target
    requested: str  # auto | an option id
    used: str
    label: str
    classic: str  # the option a mid-run fallback switches to
    classic_label: str
    explicit: bool
    why: str | None = None  # auto: why a faster option was not used

    def public(self) -> dict[str, Any]:
        return {"requested": self.requested, "used": self.used, "label": self.label, "explicit": self.explicit,
                "why": self.why}


@dataclass
class TargetCtx:
    """What a target-side check sees (the source side gets the ReadRequest)."""

    table: str
    cols: list
    mode: str
    keys: list[str]
    exists: bool


@lru_cache
def importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def options_of(type_cls_or_connector) -> tuple:
    return tuple(getattr(type_cls_or_connector, "drivers", ()) or ())


def check_setting(type_cls, value: str | None) -> str | None:
    """A connection's `driver` value: None if it is fine, else the one-line reason."""
    opts = options_of(type_cls)
    if value is None:
        return None
    if not opts:
        return f"{type_cls.label} connections have one driver only"
    ids = [o.id for o in opts]
    if value != AUTO and value not in ids:
        return f"driver must be auto or one of: {', '.join(ids)}"
    return None


def default_setting(type_cls) -> str | None:
    """What a new connection stores: auto where there is a choice."""
    return AUTO if options_of(type_cls) else None


def _serves(o, side: str) -> bool:
    return o.read if side == "source" else o.write


def problem(adapter, option, side: str, ctx: Any) -> str | None:
    if option.classic:
        return None
    if option.module and not importable(option.module):
        return f"the {option.module} package is not installed here"
    return adapter.driver_problem(option.id, side, ctx)


def choose(adapter, requested: str | None, side: str, ctx: Any) -> Choice | None:
    """The option for one side (None: the database has one driver). Raises DriverRefused for an explicit option
    that fails its checks, or an unknown id."""
    opts = options_of(adapter.c)
    if not opts:
        return None
    classic = next(o for o in opts if o.classic)
    req = requested or classic.id
    by_id = {o.id: o for o in opts}
    if req != AUTO and req not in by_id:
        raise DriverRefused(f"{side} driver {req!r} is not one of: auto, {', '.join(by_id)}")

    def made(o, explicit: bool, why: str | None = None) -> Choice:
        return Choice(side, req, o.id, o.label, classic.id, classic.label, explicit, why)

    if req == AUTO:
        why = None
        for o in opts:
            if o.classic or not _serves(o, side):
                continue
            p = problem(adapter, o, side, ctx)
            if p is None:
                return made(o, False)
            why = why or f"{side}: using {classic.label} — {p}"
        return made(classic, False, why)
    o = by_id[req]
    if not _serves(o, side):  # e.g. a read-only option on the target side: its write half is the classic one
        return made(classic, True, f"{side}: {o.label} only reads; using {classic.label}")
    p = problem(adapter, o, side, ctx)
    if p is not None:
        raise DriverRefused(f"{side} driver {o.label} can't be used: {p}")
    return made(o, True)
