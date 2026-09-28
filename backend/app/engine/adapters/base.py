"""Adapter interface: everything the engine needs from one database to read from it or write to it.

Connectors (app/connectors) own connecting, testing and catalog inspection. Adapters own moving data:
splitting a table into parallel slices, streaming Arrow batches out, and writing batches in.
"""
from __future__ import annotations

import datetime as dt
import decimal
import math
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from ..ddl import TargetOptions, qi, qualified
from ..model import Batch, ReadRequest, SplitSpec
from ..types import ColSpec, clean


class PartialWrite(Exception):
    """Some rows of a batch were rejected by the target while the rest were written (e.g. MongoDB)."""

    def __init__(self, failed: dict[int, str]):
        super().__init__(f"{len(failed)} rows rejected")
        self.failed = failed  # row index in the batch -> reason


class Writer(ABC):
    """One target connection. Each write() is atomic (own transaction) and must be safe to retry."""

    @abstractmethod
    def write(self, table: pa.Table) -> None: ...

    def close(self) -> None:  # pragma: no cover - trivial default
        pass


@dataclass
class SqlDialect:
    name: str
    param: str  # placeholder text: "%s" or "?"
    escape_percent: bool  # drivers that use %s need literal % doubled in user SQL

    def q(self, ident: str) -> str:
        return qi(self.name, ident)


def _esc(d: SqlDialect):
    return (lambda s: s.replace("%", "%%")) if d.escape_percent else (lambda s: s)


def source_sql(d: SqlDialect, req: ReadRequest) -> str:
    if req.query:
        return f"({_esc(d)(req.query.rstrip().rstrip(';'))}) AS conduit_q"
    return qualified(d.name, req.table or "")


def where_conds(d: SqlDialect, req: ReadRequest) -> list[str]:
    return [f"({_esc(d)(req.where.strip())})"] if req.where.strip() else []


def bound_conds(d: SqlDialect, req: ReadRequest) -> tuple[list[str], list[Any]]:
    """Job-level ID/time bounds on the split column (splits of kind 'range' carry their own, tighter, bounds)."""
    conds: list[str] = []
    params: list[Any] = []
    if req.split_col and req.mode != "full" and req.split_col != "ctid":
        col = d.q(req.split_col)
        if req.lo is not None:
            conds.append(f"{col} {'>' if req.exclusive_lo else '>='} {d.param}")
            params.append(req.lo)
        if req.hi is not None:
            conds.append(f"{col} < {d.param}")
            params.append(req.hi)
    return conds, params


def _tail(conds: list[str]) -> str:
    return " WHERE " + " AND ".join(conds) if conds else ""


def build_read_sql(d: SqlDialect, req: ReadRequest, split: SplitSpec, exprs: dict[str, str] | None = None,
                   extra: list[str] | None = None) -> tuple[str, list[Any]]:
    """SELECT for one split. Range splits are ordered by their column so a retry can resume after the last key.

    `exprs` maps a column name to a SQL expression that replaces it in the select list (type conversions);
    `extra` are additional WHERE conditions with no parameters (block ranges, partition lists).
    """
    exprs = exprs or {}
    cols = ", ".join(f"{exprs[c.name]} AS {d.q(c.name)}" if c.name in exprs else d.q(c.name) for c in req.columns)
    conds = where_conds(d, req) + list(extra or [])
    params: list[Any] = []
    if split.kind == "range" and split.column:
        col = d.q(split.column)
        if split.lo is not None:
            conds.append(f"{col} {'>' if split.exclusive_lo else '>='} {d.param}")
            params.append(split.lo)
        if split.hi is not None:
            conds.append(f"{col} < {d.param}")
            params.append(split.hi)
        resume = split.extra.get("resume_after")  # unique key: continue strictly after the last committed value
        if resume is not None:
            conds.append(f"{col} > {d.param}")
            params.append(resume)
        resume_from = split.extra.get("resume_from")  # non-unique key (upsert only): re-read the boundary value
        if resume_from is not None:
            conds.append(f"{col} >= {d.param}")
            params.append(resume_from)
    else:
        bc, bp = bound_conds(d, req)
        conds += bc
        params += bp
    sql = f"SELECT {cols} FROM {source_sql(d, req)}{_tail(conds)}"
    if split.kind == "range" and split.column:
        sql += f" ORDER BY {d.q(split.column)}"
    return sql, params


def _filtered(d: SqlDialect, req: ReadRequest, select: str) -> tuple[str, list[Any]]:
    conds = where_conds(d, req)
    bc, params = bound_conds(d, req)
    return f"SELECT {select} FROM {source_sql(d, req)}{_tail(conds + bc)}", params


def build_count_sql(d: SqlDialect, req: ReadRequest) -> tuple[str, list[Any]]:
    return _filtered(d, req, "COUNT(*)")


def build_minmax_sql(d: SqlDialect, req: ReadRequest) -> tuple[str, list[Any]]:
    col = d.q(req.split_col or "")
    return _filtered(d, req, f"MIN({col}), MAX({col}), COUNT(*)")


def build_probe_sql(d: SqlDialect, req: ReadRequest) -> tuple[str, list[Any]]:
    """Cheap statement whose plan row estimate stands for the whole request (EXPLAIN target)."""
    return _filtered(d, req, "1")


# ---- boundaries for range splitting ---------------------------------------------

def next_after(v: Any) -> Any:
    """Smallest value greater than v (used to make max() an exclusive upper bound)."""
    if isinstance(v, bool):
        return int(v) + 1
    if isinstance(v, int):
        return v + 1
    if isinstance(v, float):
        return math.nextafter(v, math.inf)
    if isinstance(v, decimal.Decimal):
        return v + decimal.Decimal(1).scaleb(min(v.as_tuple().exponent, 0))
    if isinstance(v, dt.datetime):
        return v + dt.timedelta(microseconds=1)
    if isinstance(v, dt.date):
        return v + dt.timedelta(days=1)
    return v


def equal_bounds(lo: Any, hi_excl: Any, n: int) -> list[Any]:
    """n+1 boundaries from lo to hi_excl, equal width, strictly increasing."""
    if n <= 1 or lo is None or hi_excl is None or not hi_excl > lo:
        return [lo, hi_excl]
    out = [lo]
    if isinstance(lo, dt.datetime):
        step = (hi_excl - lo) / n
        pts = [lo + step * i for i in range(1, n)]
    elif isinstance(lo, dt.date):
        days = (hi_excl - lo).days
        pts = [lo + dt.timedelta(days=days * i // n) for i in range(1, n)]
    elif isinstance(lo, int) and isinstance(hi_excl, int):
        pts = [lo + (hi_excl - lo) * i // n for i in range(1, n)]
    else:
        lo_d, hi_d = decimal.Decimal(str(lo)), decimal.Decimal(str(hi_excl))
        pts = [type(lo)(lo_d + (hi_d - lo_d) * i / n) if not isinstance(lo, float) else float(lo_d + (hi_d - lo_d) * i / n)
               for i in range(1, n)]
    for p in pts:
        if p > out[-1]:
            out.append(p)
    if hi_excl > out[-1]:
        out.append(hi_excl)
    return out


def range_label(col: str, lo: Any, hi: Any) -> str:
    def f(v: Any) -> str:
        if isinstance(v, dt.datetime):
            return v.strftime("%Y-%m-%d %H:%M") if v.second == 0 else v.strftime("%Y-%m-%d %H:%M:%S")
        return f"{v:,}" if isinstance(v, int) else str(v)
    if lo is None and hi is None:
        return "all rows"
    return f"{col} {f(lo) if lo is not None else '…'} … {f(hi) if hi is not None else '…'}"


class Adapter(ABC):
    dialect: str
    max_writers: int = 16  # concurrent writers the target tolerates (SQLite/DuckDB: 1)
    sql: SqlDialect

    def __init__(self, connector):
        self.c = connector
        self.notes = []  # planner remarks shown to the user (fallbacks, version limits ...)

    # ---- schema ----------------------------------------------------------------
    @abstractmethod
    def table_columns(self, table: str) -> list[ColSpec]:
        """Logical columns of an existing table/collection (empty list if it does not exist)."""

    @abstractmethod
    def query_columns(self, sql: str) -> list[ColSpec]:
        """Logical columns a SELECT would return (zero-row probe)."""

    # ---- source ----------------------------------------------------------------
    @abstractmethod
    def estimate_rows(self, req: ReadRequest, exact: bool = False) -> tuple[int | None, bool]:
        """(row count, is_exact). Cheap catalog estimate unless exact=True (then COUNT with the filter)."""

    @abstractmethod
    def key_range(self, req: ReadRequest) -> tuple[Any, Any] | None:
        """min and max of req.split_col under the request's filter; None when there are no rows."""

    notes: list[str]

    def plan_splits(self, req: ReadRequest, n: int) -> list[SplitSpec]:
        """Default: equal-width ranges on req.split_col between the real min and max (already inside the job bounds).

        Even one split is a range split when there is a split column, so a retry can resume after the last key.
        """
        if not req.split_col:
            return [SplitSpec(0, "all rows", "full")]
        if not self.is_splittable(req.split_col_logical):
            self.notes.append(f"{req.split_col} is not a numeric or date/time column, so it cannot be range-split; reading in one piece.")
            return [SplitSpec(0, "all rows", "full")]
        rng = self.key_range(req)
        if rng is None or rng[0] is None:
            return [SplitSpec(0, "all rows (empty)", "full")]
        mn, mx = rng
        return self.ranges_from_bounds(req, equal_bounds(mn, self.after(mx), max(1, n)))

    def ranges_from_bounds(self, req: ReadRequest, bounds: list[Any], total: int | None = None) -> list[SplitSpec]:
        if len(bounds) < 2:
            return [SplitSpec(0, "all rows", "full")]
        k = len(bounds) - 1
        return [
            SplitSpec(idx=i, label=range_label(req.split_col or "", bounds[i], bounds[i + 1]), kind="range",
                      column=req.split_col, lo=bounds[i], hi=bounds[i + 1],
                      est_rows=(total // k if total else None), resumable=True)
            for i in range(k)
        ]

    @abstractmethod
    def read_batches(self, req: ReadRequest, split: SplitSpec, batch_rows: int) -> Iterator[Batch]: ...

    # ---- target ----------------------------------------------------------------
    @abstractmethod
    def prepare_target(self, table: str, cols: list[ColSpec], mode: str, keys: list[str], opts: TargetOptions,
                       create: bool, execute: bool) -> list[str]:
        """Create the table if missing and truncate for replace mode. Returns the statements it ran (or would run)."""

    @abstractmethod
    def open_writer(self, table: str, cols: list[ColSpec], mode: str, keys: list[str]) -> Writer: ...

    # ---- helpers ---------------------------------------------------------------
    def is_unique(self, table: str, column: str) -> bool:
        """True when `column` alone is a PRIMARY KEY / UNIQUE column, so it can be used to resume a split exactly."""
        return False

    def after(self, v: Any) -> Any:
        """Smallest value above v: the exclusive upper bound that still includes v."""
        return next_after(v)

    def is_splittable(self, logical: str | None) -> bool:
        t = clean(logical or "")
        return t in ("SMALLINT", "INTEGER", "BIGINT", "FLOAT", "DOUBLE", "DATE", "TIMESTAMP", "TIMESTAMPTZ") or t.startswith("DECIMAL")

    def is_time(self, logical: str | None) -> bool:
        return clean(logical or "") in ("DATE", "TIMESTAMP", "TIMESTAMPTZ")
