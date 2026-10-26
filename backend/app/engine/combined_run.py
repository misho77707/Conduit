"""Running a combined pipeline source inside its job (see engine/combined.py for the model, checks and preview).

Staged mode, per job, in <data>/combined/jobs/<job id>/:
  1. every input is staged into scratch.duckdb (engine/staging.py, in parallel; the driver only above its saved
     watermark, minus the overlap) while the free disk space stays above a floor;
  2. the SQL runs once: CREATE TABLE result AS SELECT row_number() OVER () AS __conduit_row, * FROM (<sql>);
     the staged inputs are dropped, state.json records that the join is done (and the driver's new watermark);
  3. `result` is loaded by StagedResult: row ranges of __conduit_row, which resume exactly after a failure.
A Retry finds state.json and goes straight to (3): the sources are never read twice. The folder is deleted when
the job succeeds; a failed or cancelled job keeps it until it is pruned or deleted.

Stream mode: see stream_source() (lookups in memory, the driver streamed, the SQL per driver batch).
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
import math
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..runtime import current_settings
from ..db import get_sessionmaker
from ..filecache import NotEnoughSpace
from ..models import Job, Watermark, utcnow
from . import joblog
from .arrowconv import conform
from .combined import CombinedError, open_input, result_columns
from .filesource import FileAdapter, MultiFileAdapter
from .model import Batch, ReadRequest, SplitSpec, dec, enc
from .spec import CombinedIn, JobIn
from .staging import StageInput, StagingCancelled, StagingError, qi, read_request, stage
from .types import ColSpec, clean

ROW = "__conduit_row"
ROWS_PER_SPLIT = 1_000_000  # the staged result is loaded in row ranges of about this size, `workers` at a time





def job_dir(job_id: uuid.UUID | str) -> Path:
    return Path(get_settings().combined_dir) / "jobs" / str(job_id)


def remove_job_dir(job_id: uuid.UUID | str) -> None:
    shutil.rmtree(job_dir(job_id), ignore_errors=True)


# ---- disk floor ----------------------------------------------------------------------------------

def _floor_bytes(path: Path) -> tuple[int, int, int]:
    """(free, floor, total) for the disk `path` is on. The floor is the larger of the GB and % settings."""
    st = current_settings()
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    du = shutil.disk_usage(probe)
    floor = max(int(st.combined_min_free_gb * 1024 ** 3), int(du.total * st.combined_min_free_pct / 100))
    return du.free, floor, du.total


def check_floor(path: Path, what: str) -> None:
    free, floor, _ = _floor_bytes(path)
    if free < floor:
        gb = 1024 ** 3
        raise NotEnoughSpace(f"Stopped {what}: the data folder is down to {free / gb:,.1f} GB free, below the "
                             f"{floor / gb:,.1f} GB floor (Settings: Keep free on the data disk). Free some space, read fewer "
                             "rows or columns per input (WHERE / column subset), or use stream mode.")


# ---- progress ------------------------------------------------------------------------------------

def _progress(job_id: uuid.UUID, phase: str) -> bool:
    """Record the phase and keep the heartbeat fresh; True when a cancel was requested."""
    with get_sessionmaker()() as s:
        s.execute(update(Job).where(Job.id == job_id).values(phase=phase[:200], heartbeat_at=utcnow()))
        s.commit()
        return bool(s.scalar(select(Job.cancel_requested).where(Job.id == job_id)))


def _fmt(n: int | None) -> str:
    if n is None:
        return "?"
    return f"{n / 1e6:,.1f}M" if n >= 1_000_000 else f"{n / 1e3:,.0f}k" if n >= 10_000 else f"{n:,}"


def _staging_phase(rows: dict[str, int], est: dict[str, int | None]) -> str:
    return "staging " + " · ".join(f"{a} {_fmt(n)}{f' / ~{_fmt(est[a])}' if est.get(a) else ''}" for a, n in rows.items())


# ---- the watermark on the driver --------------------------------------------------------------------

def watermark_key(job: Job, c: CombinedIn) -> tuple[str, str, str]:
    owner = f"pipeline:{job.pipeline_id}" if job.pipeline_id else f"job:{job.id}"
    t = job.spec.get("target") or {}
    return f"{owner}:{c.driver}", f"{t.get('connection_id')}:{t.get('table')}", c.watermark.column


def saved_watermark(session: Session, job: Job, c: CombinedIn) -> Any:
    sk, tk, col = watermark_key(job, c)
    wm = session.scalar(select(Watermark).where(Watermark.source_key == sk, Watermark.target_key == tk, Watermark.column == col))
    return dec(wm.value) if wm is not None else None


def save_watermark(session: Session, job: Job, c: CombinedIn, value: Any) -> None:
    sk, tk, col = watermark_key(job, c)
    wm = session.scalar(select(Watermark).where(Watermark.source_key == sk, Watermark.target_key == tk, Watermark.column == col))
    if wm is None:
        session.add(Watermark(source_key=sk, target_key=tk, column=col, value=enc(value)))
    else:
        wm.value = enc(value)
    joblog.push(job.id, "info", f"saved watermark {c.driver}.{col} = {value}")


def _minus_overlap(value: Any, overlap: float) -> Any:
    if not overlap or value is None:
        return value
    if isinstance(value, dt.datetime):
        return value - dt.timedelta(seconds=overlap)
    if isinstance(value, dt.date):
        return value - dt.timedelta(days=math.ceil(overlap / 86400))
    if isinstance(value, decimal.Decimal):
        return value - decimal.Decimal(str(overlap))
    if isinstance(value, int):
        return value - int(math.ceil(overlap))
    return value - overlap


def _watermark_column(c: CombinedIn, cols: list[ColSpec]) -> ColSpec:
    col = next((x for x in cols if x.name == c.watermark.column), None)
    if col is None:
        raise CombinedError(f"The watermark column {c.watermark.column} is not one of {c.driver}'s columns "
                            f"({', '.join(x.name for x in cols[:12])})")
    if clean(col.logical) not in ("SMALLINT", "INTEGER", "BIGINT", "FLOAT", "DOUBLE", "DATE", "TIMESTAMP", "TIMESTAMPTZ") \
            and not clean(col.logical).startswith("DECIMAL"):
        raise CombinedError(f"The watermark column {c.driver}.{col.name} is {col.logical}: it must be a number or a date/time")
    return col


def driver_request(c: CombinedIn, adapter: Any, src, col: ColSpec, lo: Any) -> ReadRequest | None:
    """Rows of the driver above `lo`, pushed down to a database; None for a file (filtered after reading)."""
    if lo is None or isinstance(adapter, (FileAdapter, MultiFileAdapter)):
        return None
    if clean(col.logical) == "TIMESTAMPTZ" and isinstance(lo, dt.datetime) and lo.tzinfo is None:
        lo = lo.replace(tzinfo=dt.timezone.utc)  # staged times are naive UTC
    mode = "time" if clean(col.logical) in ("DATE", "TIMESTAMP", "TIMESTAMPTZ") else "id"
    return ReadRequest(table=src.table, query=src.query, where=src.where, mode=mode, split_col=col.name,
                       split_col_logical=col.logical, lo=lo, exclusive_lo=True)


def estimate(si: StageInput) -> int | None:
    """A cheap row estimate of one input (catalog / file metadata), only for the progress line."""
    try:
        if isinstance(si.adapter, (FileAdapter, MultiFileAdapter)):
            return si.adapter.estimate_rows()[0]
        return si.adapter.estimate_rows(read_request(si))[0]
    except Exception:  # noqa: BLE001
        return None


# ---- staged mode -----------------------------------------------------------------------------------

def job_source(session: Session, spec: JobIn, job_id: uuid.UUID) -> Any:
    c = spec.source.combined
    job = session.get(Job, job_id)
    if c.mode == "stream":
        from .combined_stream import stream_source

        return stream_source(session, spec, job)
    d = job_dir(job_id)
    scratch, state_path = d / "scratch.duckdb", d / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if state.get("joined") and scratch.exists():
        joblog.push(job_id, "info", "reusing the staged inputs and the join result of the first attempt (no source is read again)")
    else:
        state = _stage_and_join(session, c, job, d, scratch, spec.options.read_chunk)
        state_path.write_text(json.dumps(state), encoding="utf-8")
    _progress(job_id, "loading")
    return StagedResult(scratch, c, state)


def _stage_and_join(session: Session, c: CombinedIn, job: Job, d: Path, scratch: Path, chunk: int) -> dict[str, Any]:
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    check_floor(d, "before staging")
    lo = wm_col = None
    if c.watermark:
        prev = saved_watermark(session, job, c)
        lo = _minus_overlap(prev, c.watermark.overlap)
        joblog.push(job.id, "info", f"watermark: reading {c.driver} rows with {c.watermark.column} > {lo}" if prev is not None
                    else f"watermark: first run, reading all of {c.driver}")
    inputs, est = [], {}
    for inp in c.inputs:
        src, cols = open_input(session, inp, full_dir=d / "remote" / inp.alias)
        req = None
        if c.watermark and inp.alias == c.driver:
            wm_col = _watermark_column(c, cols)
            req = driver_request(c, src, inp.source, wm_col, lo)
        si = StageInput(inp.alias, src, inp.source, cols, label=f"input {inp.alias}", request=req)
        est[inp.alias] = estimate(si)
        inputs.append(si)
    labels = {i.alias: i for i in c.inputs}
    last = [0.0]

    def guard(si: StageInput) -> None:
        now = time.monotonic()
        if now - last[0] >= 1.0:  # a disk_usage call per second is plenty
            last[0] = now
            check_floor(d, f"staging {si.table}")

    con = duckdb.connect(str(scratch))
    try:
        con.execute("SET preserve_insertion_order = false")
        try:
            res = stage(con, inputs, chunk=chunk, tick=lambda n: _progress(job.id, _staging_phase(n, est)), guard=guard)
        except StagingError as e:
            raise CombinedError(str(e)) from e
        for alias, n in res.rows.items():
            joblog.push(job.id, "info", f"staged {alias}: {n:,} rows")
        new_wm = None
        if c.watermark and wm_col is not None:
            if lo is not None and isinstance(next(i.adapter for i in inputs if i.table == c.driver), (FileAdapter, MultiFileAdapter)):
                con.execute(f"DELETE FROM {qi(c.driver)} WHERE NOT ({qi(wm_col.name)} > ?)", [lo])  # files: filter here
            new_wm = con.execute(f"SELECT max({qi(wm_col.name)}) FROM {qi(c.driver)}").fetchone()[0]
        if _progress(job.id, "joining"):
            raise StagingCancelled()
        joblog.push(job.id, "info", "joining")
        try:
            con.execute(f"CREATE TABLE result AS SELECT row_number() OVER () AS {ROW}, * FROM ({c.sql}) AS conduit_join")
        except duckdb.Error as e:
            raise CombinedError(f"The SQL failed on the staged inputs: {' '.join(str(e).split())}") from e
        result_columns(con.sql(f"SELECT * EXCLUDE ({ROW}) FROM result"))  # duplicate names: a clear error
        n = int(con.execute("SELECT count(*) FROM result").fetchone()[0])
        for alias in labels:  # the inputs are not needed any more: give their blocks back to the file
            con.execute(f"DROP TABLE {qi(alias)}")
        con.execute("CHECKPOINT")
    finally:
        con.close()
    joblog.push(job.id, "info", f"join done: {n:,} rows to load")
    return {"joined": True, "rows": res.rows, "estimates": est, "result_rows": n,
            "watermark": enc(new_wm) if new_wm is not None else None}


class StagedResult:
    """The joined result of a staged combined source, loaded in row ranges that resume exactly (by __conduit_row)."""

    dialect = "combined"
    max_writers = 0

    def __init__(self, scratch: Path, c: CombinedIn, state: dict[str, Any]) -> None:
        self.scratch, self.c, self.state = scratch, c, state
        self.rows = int(state.get("result_rows") or 0)
        con = duckdb.connect(str(scratch), read_only=True)
        try:
            self.columns = result_columns(con.sql(f"SELECT * EXCLUDE ({ROW}) FROM result"))
        finally:
            con.close()
        staged = ", ".join(f"{a} {n:,} rows" for a, n in (state.get("rows") or {}).items())
        self.notes = [f"Combined ({c.mode}): {staged}; joined into {self.rows:,} rows."]
        if c.watermark:
            self.notes.append(f"Watermark {c.driver}.{c.watermark.column}: "
                              + (f"the new value {dec(state['watermark'])} is saved when the load succeeds"
                                 if state.get("watermark") is not None else "no new driver rows, the saved value stays"))

    def read_columns(self) -> list[ColSpec]:
        return list(self.columns)

    def suggest_columns(self) -> list[ColSpec]:
        return list(self.columns)

    def estimate_rows(self, req: ReadRequest | None = None, exact: bool = False) -> tuple[int | None, bool]:
        return self.rows, True

    def key_range(self, req: ReadRequest) -> None:
        return None

    def is_splittable(self, logical: str | None) -> bool:
        return False

    def plan_splits(self, req: ReadRequest, n: int) -> list[SplitSpec]:
        k = max(1, min(32, math.ceil(self.rows / ROWS_PER_SPLIT)))
        bounds = [round(self.rows * i / k) for i in range(k + 1)]
        return [SplitSpec(i, f"rows {bounds[i] + 1:,}–{bounds[i + 1]:,}" if self.rows else "no rows", "file",
                          est_rows=bounds[i + 1] - bounds[i], resumable=True, extra={"lo": bounds[i], "hi": bounds[i + 1]})
                for i in range(k)]

    def read_batches(self, req: ReadRequest, split: SplitSpec, batch_rows: int):
        start = max(int(split.extra.get("offset") or 0), int(split.extra.get("lo") or 0))  # offset: last committed row
        hi = int(split.extra.get("hi") or self.rows)
        cols = ", ".join(qi(col.name) for col in req.columns)
        con = duckdb.connect(str(self.scratch), read_only=True)
        try:
            res = con.execute(f"SELECT {cols}, {ROW} FROM result WHERE {ROW} > ? AND {ROW} <= ? ORDER BY {ROW}", [start, hi])
            for rb in res.fetch_record_batch(batch_rows):
                if not rb.num_rows:
                    continue
                t = pa.Table.from_batches([rb])
                last = t.column(ROW)[-1].as_py()
                yield Batch(conform(t.drop_columns([ROW]), req.columns), last)
        finally:
            con.close()

    def finish(self, session: Session, job: Job) -> None:
        """The load succeeded: remember the driver's new watermark."""
        if self.c.watermark and self.state.get("watermark") is not None:
            save_watermark(session, job, self.c, dec(self.state["watermark"]))
