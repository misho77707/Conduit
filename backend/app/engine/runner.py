"""The load engine: plan a job, cut it into splits, run them in parallel, report progress.

Flow of one split:   reader (own thread, prefetching)  ->  transform  ->  re-batch  ->  writer
A failed batch is bisected so one bad row does not sink 50,000 good ones; bad rows go to a rejects file.
Progress lives in the app database (per split, atomic increments), so the API can read it from anywhere
and a crashed job can continue from the last committed batch of each split.
"""
from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import math
import queue
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..runtime import current_settings
from ..db import get_engine, get_sessionmaker
from ..models import Connection, Job, JobSplit, Watermark, utcnow
from ..connectors import category_of
from ..connectors.base import UserFacingError
from ..services import build_connector
from . import joblog
from .adapters import PartialWrite, get_adapter
from .arrowconv import _jsonable
from .ddl import TargetOptions, split_statements, suggest_partition
from ..files import UnsupportedFormat
from .combined import PlannedCombined
from .filesource import FileAdapter, FileSpec, MultiFileAdapter
from .model import ReadRequest, SplitSpec, dec, enc
from .spec import ColumnMap, JobIn, parse_bound
from .steps import Step, apply_steps, compile_steps
from .transform import NO_FILL, OutCol, Reject, Transformer
from .types import ColSpec, clean

log = logging.getLogger("conduit.engine")

EXACT_COUNT_MAX_ROWS = 20_000_000  # below this, a filtered request is counted exactly so the progress bar is honest
RATE_WINDOW_S = 30.0  # ETA uses the average speed over this window
SYSTEMATIC_REJECTS = 20  # this many rejects with nothing ever written: the target, not the data, is the problem


# ---- errors ----------------------------------------------------------------------

class JobError(Exception):
    """The request cannot run as given (bad table, column, option). Message is shown to the user."""


class Cancelled(Exception):
    pass


class TooManyRejects(Exception):
    pass


class SystematicError(Exception):
    pass


_ROW_ERRORS = {
    "DataError", "IntegrityError", "ConstraintException", "ConversionException", "InvalidInputException",
    "OverflowError", "UnicodeError", "ValueError", "TypeError", "ArrowInvalid", "ArrowTypeError", "ConversionError",
}
_CH_ROW_HINTS = ("CANNOT_PARSE", "CANNOT_CONVERT", "VALUE_IS_OUT_OF_RANGE", "ARGUMENT_OUT_OF_BOUND", "TYPE_MISMATCH",
                 "CANNOT_INSERT_NULL", "TOO_LARGE_STRING_SIZE", "DECIMAL_OVERFLOW", "Cannot convert", "out of range")
_TRANSIENT = {
    "OperationalError", "InterfaceError", "ConnectionError", "TimeoutError", "BrokenPipeError", "ConnectionResetError",
    "ServerSelectionTimeoutError", "AutoReconnect", "NetworkTimeout", "ConnectTimeout", "ReadTimeout", "ProtocolError",
    "MaxRetryError", "IOException",
}
_TRANSIENT_TEXT = ("deadlock", "lock wait timeout", "server has gone away", "connection reset", "connection refused",
                   "too many connections", "timed out", "broken pipe", "could not connect", "ssl syscall", "connection closed",
                   "lost connection", "try again", "serialization failure")
_NOT_TRANSIENT_TEXT = ("password authentication failed", "access denied", "does not exist", "unknown database", "permission denied")


def _names(e: BaseException) -> set[str]:
    return {c.__name__ for c in type(e).__mro__}


def is_row_error(e: BaseException) -> bool:
    if _names(e) & _ROW_ERRORS:
        return True
    return "DatabaseError" in _names(e) and any(h in str(e) for h in _CH_ROW_HINTS)


def is_transient(e: BaseException) -> bool:
    text = str(e).lower()
    if any(t in text for t in _NOT_TRANSIENT_TEXT):
        return False
    return bool(_names(e) & _TRANSIENT) or any(t in text for t in _TRANSIENT_TEXT)


def short_error(e: BaseException) -> str:
    msg = " ".join(str(e).split())
    if isinstance(e, (JobError, UserFacingError)):  # already written for people: no exception class name
        return msg[:400]
    return f"{type(e).__name__}: {msg}"[:400]


# ---- reader prefetch + rejects -----------------------------------------------------

_END = object()


class Prefetcher:
    """Runs a batch generator in its own thread so reading overlaps with transforming and writing."""

    def __init__(self, gen, stop: threading.Event, depth: int = 2):
        self.gen, self.stop, self.q = gen, stop, queue.Queue(maxsize=depth)
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="conduit-reader")
        self.thread.start()

    def _put(self, item: Any) -> bool:
        while not (self.closed.is_set() or self.stop.is_set()):
            try:
                self.q.put(item, timeout=0.3)
                return True
            except queue.Full:
                continue
        return False

    def _run(self) -> None:
        try:
            for b in self.gen:
                if not self._put(b):
                    return
            self._put(_END)
        except BaseException as e:  # noqa: BLE001 - handed to the consumer
            self._put(e)
        finally:
            try:
                self.gen.close()
            except Exception:  # noqa: BLE001
                pass

    def __iter__(self):
        while True:
            try:
                item = self.q.get(timeout=0.3)
            except queue.Empty:
                if self.stop.is_set() or self.closed.is_set():
                    raise Cancelled() from None
                if not self.thread.is_alive() and self.q.empty():
                    return
                continue
            if item is _END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    def close(self) -> None:
        self.closed.set()
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass
        self.thread.join(timeout=10)


class RejectSink:
    """Thread-safe CSV of rows that were not loaded: split, reason, row (JSON)."""

    def __init__(self, path: Path, cap: int):
        self.path, self.cap, self.count = path, cap, 0
        self._lock = threading.Lock()
        self._f = None
        self._w = None

    def add(self, split: int, reason: str, row: dict[str, Any]) -> None:
        with self._lock:
            if self._f is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                new = not self.path.exists() or self.path.stat().st_size == 0
                self._f = self.path.open("a", newline="", encoding="utf-8")
                self._w = csv.writer(self._f)
                if new:
                    self._w.writerow(["split", "reason", "row"])
            self._w.writerow([split, reason, json.dumps(row, ensure_ascii=False, default=_jsonable)])
            self.count += 1
            if self.count % 500 == 0:
                self._f.flush()

    @property
    def exceeded(self) -> bool:
        return self.cap >= 0 and self.count > self.cap

    def close(self) -> None:
        with self._lock:
            if self._f:
                self._f.close()
                self._f = None


def rejects_path(job_id: uuid.UUID | str) -> Path:
    return Path(get_settings().rejects_dir) / f"{job_id}.rejects.csv"


# ---- preparing a request -----------------------------------------------------------

@dataclass
class Prepared:
    spec: JobIn
    src: Any  # Adapter | FileAdapter | MultiFileAdapter
    tgt: Any  # Adapter
    req: ReadRequest
    out: list[OutCol]
    tf: Transformer
    target_cols: list[ColSpec]
    target_exists: bool
    unique_split: bool
    split_logical: str | None
    src_key: str
    tgt_key: str
    notes: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    remote: Any = None  # remote.RemoteCtx of a load job reading downloaded remote files

    @property
    def opts(self):
        return self.spec.options


def is_file_source(src: Any) -> bool:
    """A local file, one or more remote files, or a combined source: read in one piece per file / row range."""
    return isinstance(src, (FileAdapter, MultiFileAdapter)) or getattr(src, "dialect", "") == "combined"


def combined_job_source(session: Session, spec: JobIn, job_id: uuid.UUID) -> Any:
    from .combined_run import job_source

    return job_source(session, spec, job_id)


def _conn(session: Session, cid: uuid.UUID | None, role: str) -> Connection:
    conn = session.get(Connection, cid) if cid else None
    if conn is None:
        raise JobError(f"The {role} connection no longer exists")
    if category_of(conn.type) != "database":
        raise JobError(f"{conn.name} is a file storage connection ({conn.type}): it cannot be a {role} table")
    return conn


def prepare(spec: JobIn, session: Session, job_id: uuid.UUID | None = None) -> Prepared:
    """Validate a request and open its source and target. `job_id`: preparing a real run, so a remote source is
    downloaded (once per job; a retry reuses the files) - without it, remote files are only previewed."""
    o, s, t = spec.options, spec.source, spec.target
    notes: list[str] = []
    rctx = None
    # ---- source
    if s.is_file and o.strategy != "full":
        raise JobError("Files are always loaded whole: use the 'full' strategy")
    if s.file:
        src: Any = FileAdapter(FileSpec(**s.file.model_dump()))
        read_cols, suggest = src.read_columns(), {c.name: c for c in src.suggest_columns()}
        src_key = f"file:{s.file.path}"
    elif s.combined:
        if o.strategy != "full" or o.split_column:
            raise JobError("A combined source is read as a whole: use the 'full' strategy without a split column")
        _conn(session, t.connection_id, "target")
        if job_id is None:  # launching / validating: never stage here, plan with the cached join schema
            src = PlannedCombined(spec.source_columns or [])
        else:
            src = combined_job_source(session, spec, job_id)
        notes += src.notes
        read_cols, suggest = src.read_columns(), {c.name: c for c in src.suggest_columns()}
        src_key = "combined:" + hashlib.sha1(spec.source.combined.sql.encode()).hexdigest()[:12]
    elif s.remote:
        from .. import remote as rmt

        _conn(session, t.connection_id, "target")  # before downloading anything
        tkey = rmt.target_key(t)
        try:
            if job_id is not None:
                src, rctx = rmt.job_source(session, job_id, s.remote, tkey, t.mode)
            else:
                src = rmt.preview_source(session, s.remote, tkey, t.mode)
        except (UnsupportedFormat, FileNotFoundError) as e:
            raise JobError(str(e)) from e
        notes += src.notes
        read_cols, suggest = src.read_columns(), {c.name: c for c in src.suggest_columns()}
        src_key = rmt.source_key(s.remote)
    else:
        sconn = _conn(session, s.connection_id, "source")
        src = get_adapter(build_connector(sconn))
        if s.table:
            read_cols = src.table_columns(s.table)
            if not read_cols:
                raise JobError(f"Source table {s.table} was not found (or has no columns)")
        else:
            try:
                read_cols = src.query_columns(s.query or "")
            except NotImplementedError as e:
                raise JobError(str(e)) from e
            except Exception as e:  # noqa: BLE001
                raise JobError(f"The source query does not run: {short_error(e)}") from e
        suggest = {c.name: c for c in read_cols}
        src_key = f"{sconn.id}:{s.table or 'query:' + hashlib.sha1((s.query or '').encode()).hexdigest()[:12]}"
    raw_read_cols = read_cols
    if spec.steps:
        # A pipeline's steps run between the read and the target mapping below, so everything from here
        # on (column mapping, DDL, the default identity mapping) is built against what the step chain
        # produces - not the raw source - using the cached schema from the pipeline's last preview.
        # `step_output_columns` is required by JobIn whenever `steps` is non-empty (see spec.py).
        read_cols = [ColSpec(name=c.name, logical=c.type, nullable=c.nullable, source_name=c.source_name)
                    for c in (spec.step_output_columns or [])]
        suggest = {c.name: c for c in read_cols}
    by_name = {c.name: c for c in read_cols}

    # ---- target
    tconn = _conn(session, t.connection_id, "target")
    tgt = get_adapter(build_connector(tconn))
    existing = {c.name: c for c in tgt.table_columns(t.table)}
    tgt_key = f"{tconn.id}:{t.table}"
    if not existing and not t.create:
        raise JobError(f"Target table {t.table} does not exist and 'create' is off")

    # ---- column mapping
    maps = spec.columns if spec.columns is not None else [ColumnMap(source=c.name) for c in read_cols]
    out: list[OutCol] = []
    seen: set[str] = set()
    for m in maps:
        if not m.include:
            continue
        if m.source not in by_name:
            raise JobError(f"Column {m.source!r} is not in the source (has: {', '.join(list(by_name)[:12])})")
        name = m.name or m.source
        if name in seen:
            raise JobError(f"Two columns are mapped to {name!r}")
        seen.add(name)
        base = suggest.get(m.source, by_name[m.source])
        logical = clean(m.type) if m.type else base.logical
        nullable = m.nullable if m.nullable is not None else base.nullable
        default = None
        if existing:
            if name not in existing:
                raise JobError(f"Column {name!r} does not exist in target table {t.table} "
                               f"(it has: {', '.join(list(existing)[:12])})")
            ex = existing[name]
            if m.type and clean(m.type) != clean(ex.logical):
                notes.append(f"{name}: using the existing target type {ex.logical} instead of {clean(m.type)}")
            logical, nullable, default = ex.logical, ex.nullable, ex.default
        if name in t.keys:
            nullable = False
        col = ColSpec(name, logical, nullable, m.source if m.source != name else None, default=default)
        fill = tgt.default_fill(col)
        if fill is not NO_FILL:
            notes.append(f"{name}: empty values get the column default {fill!r} ({default})")
        out.append(OutCol(m.source, col, m.format, fill))
    if not out:
        raise JobError("No columns selected")
    missing = [k for k in t.keys if k not in seen]
    if missing:
        raise JobError(f"Key column(s) {', '.join(missing)} are not among the loaded columns")

    # ---- what to read and how to cut it
    split_col = o.split_column
    split_logical = None
    if split_col:
        if split_col in by_name:
            split_logical = by_name[split_col].logical
        elif split_col not in ("ctid", "_partition_id"):
            raise JobError(f"Split column {split_col!r} is not a column of the source")
    if s.is_file and split_col:
        notes.append("Files are read in one piece; the split column is ignored")
        split_col = None
    req = ReadRequest(table=s.table, query=s.query, where=s.where, mode="full", split_col=split_col,
                      split_col_logical=split_logical)
    if o.strategy in ("id", "time", "watermark"):
        if not src.is_splittable(split_logical):
            raise JobError(f"{split_col} ({split_logical}) is not a numeric or date/time column, so it cannot be used for "
                           f"'{o.strategy}' loads")
        if o.strategy == "time" and not src.is_time(split_logical):
            raise JobError(f"{split_col} is not a date/time column")
        req.mode = "time" if src.is_time(split_logical) else "id"
        if o.strategy == "watermark":
            with_wm = session.scalar(select(Watermark).where(Watermark.source_key == src_key, Watermark.target_key == tgt_key,
                                                             Watermark.column == split_col))
            if with_wm is not None:
                req.lo, req.exclusive_lo = dec(with_wm.value), True
                notes.append(f"Loading only rows with {split_col} > {req.lo} (saved from the last run)")
            else:
                req.mode = "full"
                notes.append("No saved watermark yet: the first run loads everything and remembers the highest value")
        else:
            try:
                req.lo = parse_bound(o.lo, split_logical, o.timezone, upper=False, strategy=o.strategy, after=src.after)
                req.hi = parse_bound(o.hi, split_logical, o.timezone, upper=True, strategy=o.strategy, after=src.after)
            except (ValueError, ArithmeticError) as e:
                raise JobError(f"Bad range bound: {e}") from e
    read_cols_sel = [by_name[o_.source] for o_ in out]
    if split_col and split_col in by_name and split_col not in {c.name for c in read_cols_sel}:
        read_cols_sel.append(by_name[split_col])  # read it anyway: resuming needs the key of the last row
    # A pipeline's steps may reference any raw source column, not just the ones the (post-step) target
    # mapping selects, so the source is read in full; the step chain does the narrowing, not this list.
    req.columns = raw_read_cols if spec.steps else read_cols_sel

    unique = bool(split_col and s.table and split_col in by_name and src.is_unique(s.table, split_col))
    tgt_cols = [oc.target for oc in out]
    try:  # dry validation: e.g. upsert needs a unique key / ReplacingMergeTree on an existing table
        tgt.prepare_target(t.table, tgt_cols, t.mode, t.keys, TargetOptions(order_by=t.order_by, partition_by=t.partition_by, ddl=custom_ddl(t)), t.create, False)
    except ValueError as e:
        raise JobError(str(e)) from e
    return Prepared(spec, src, tgt, req, out, Transformer(read_cols_sel, out), tgt_cols, bool(existing), unique,
                    split_logical, src_key, tgt_key, notes, steps=compile_steps(spec.steps), remote=rctx)


# ---- planning ------------------------------------------------------------------------

def _filtered(req: ReadRequest) -> bool:
    return bool(req.where.strip()) or bool(req.query) or (req.mode != "full" and (req.lo is not None or req.hi is not None))


def estimate_total(prep: Prepared) -> tuple[int | None, bool]:
    if is_file_source(prep.src):
        return prep.src.estimate_rows()
    total, exact = prep.src.estimate_rows(prep.req)
    if not exact and _filtered(prep.req) and (total is None or total <= EXACT_COUNT_MAX_ROWS):
        try:
            total, exact = prep.src.estimate_rows(prep.req, exact=True)
        except Exception as e:  # noqa: BLE001 - keep the estimate
            log.info("exact count failed, keeping estimate: %s", e)
    return total, exact


def split_count(prep: Prepared, total: int | None) -> int:
    o = prep.opts
    if not prep.req.split_col or is_file_source(prep.src):
        return 1  # (several files: one split per file, whatever this says)
    if o.split_rows and total:
        n = math.ceil(total / o.split_rows)
    elif o.splits:
        n = o.splits
    else:
        n = o.workers * 2
    return max(1, min(n, 2000))


def target_options(prep: Prepared, total: int | None) -> TargetOptions:
    t = prep.spec.target
    part = t.partition_by
    if prep.tgt.dialect == "clickhouse" and not part and not prep.target_exists:
        part = suggest_partition(prep.target_cols, total)
        if part:
            prep.notes.append(f"Target will be partitioned by {part} because the table is large")
    return TargetOptions(order_by=t.order_by, partition_by=part, est_rows=total, ddl=custom_ddl(t))


def custom_ddl(t) -> list[str] | None:
    return split_statements(t.ddl) or None if t.ddl and t.create else None


@dataclass
class Layout:
    total: int | None
    exact: bool
    splits: list[SplitSpec]
    workers: int
    notes: list[str]
    warnings: list[str]
    ddl: list[str]


def layout(prep: Prepared, *, execute_target: bool) -> Layout:
    total, exact = estimate_total(prep)
    splits = prep.src.plan_splits(prep.req, split_count(prep, total))
    for sp in splits:
        if sp.est_rows is None and total:
            sp.est_rows = total // len(splits)
    cap = current_settings().max_workers_per_job
    if prep.opts.workers > cap:
        prep.notes.append(f"Workers limited to {cap} (Settings: Workers per job, at most).")
    workers = max(1, min(prep.opts.workers, len(splits), cap))
    t = prep.spec.target
    ddl = prep.tgt.prepare_target(t.table, prep.target_cols, t.mode, t.keys, target_options(prep, total), t.create, execute_target)

    warnings: list[str] = []
    if t.mode == "replace" and prep.target_exists:
        warnings.append("Replace empties the target table first: it stays incomplete until the job finishes.")
    if t.mode == "upsert" and prep.tgt.dialect == "clickhouse":
        warnings.append("ClickHouse upserts keep old versions until a background merge: read with FINAL to see one row per key.")
    if len(splits) > 1 and not is_file_source(prep.src):
        warnings.append("Each split reads its own snapshot, so rows changing while the job runs can be missed or read twice "
                        "across split borders.")
    if splits and splits[0].resumable and splits[0].kind != "file" and not prep.unique_split and t.mode != "upsert":
        warnings.append(f"{prep.req.split_col} is not unique, so a split that fails midway cannot safely resume: it restarts "
                        "(append/replace) only if nothing was written yet.")
    if not prep.req.split_col and not is_file_source(prep.src):
        prep.notes.append("No split column chosen: the table is read in one piece. Pick one to run in parallel.")
    if prep.tgt.max_writers < workers:
        prep.notes.append(f"{prep.tgt.dialect} allows {prep.tgt.max_writers} writer at a time: reads run in parallel, writes take turns.")
    notes = prep.notes + getattr(prep.src, "notes", []) + getattr(prep.tgt, "notes", [])
    return Layout(total, exact, splits, workers, list(dict.fromkeys(notes)), warnings, ddl)


def _rows_json(table: pa.Table, n: int) -> list[dict[str, Any]]:
    return [{k: (None if v is None else _jsonable(v) if not isinstance(v, (int, float, str, bool)) else v) for k, v in r.items()}
            for r in table.slice(0, n).to_pylist()]


def make_plan(prep: Prepared, *, sample_rows: int = 2000) -> dict[str, Any]:
    """Everything the review step shows: columns, DDL, splits, estimates and a timed sample. Writes nothing."""
    lay = layout(prep, execute_target=False)
    plan: dict[str, Any] = {
        "source_columns": [c.to_dict() for c in prep.req.columns],
        "target_columns": [{**o.target.to_dict(), "source_name": o.source, "format": o.fmt} for o in prep.out],
        "target_exists": prep.target_exists, "ddl": lay.ddl, "rows_total": lay.total, "rows_total_exact": lay.exact,
        "split_column": prep.req.split_col, "split_unique": prep.unique_split, "workers": lay.workers,
        "splits": [{"idx": s.idx, "label": s.label, "est_rows": s.est_rows, "kind": s.kind} for s in lay.splits],
        "notes": lay.notes, "warnings": lay.warnings, "sample": None,
    }
    if sample_rows and lay.splits:
        t0 = time.perf_counter()
        gen = prep.src.read_batches(prep.req, lay.splits[0], sample_rows)
        first = None
        try:
            for b in gen:
                first = b
                break
        finally:
            gen.close()
        if first is not None:
            b_table = apply_steps(prep.steps, first.table) if prep.steps else first.table
            table, rej = prep.tf.apply(b_table)
            secs = max(time.perf_counter() - t0, 1e-6)
            n = first.table.num_rows + len(first.rejects)
            rate = n / secs
            plan["sample"] = {
                "rows": n, "seconds": round(secs, 3), "rows_per_s": round(rate), "rejected": len(first.rejects) + len(rej),
                "reject_examples": [{"reason": r, "row": row} for r, row in (first.rejects + rej)[:5]],
                "preview": _rows_json(table, 20),
                "est_duration_s": round(lay.total / (rate * lay.workers)) if lay.total else None,
                "basis": "reading and converting a sample with the chosen number of workers; the real write speed is measured while running",
            }
    return plan


# ---- progress ------------------------------------------------------------------------

def _bump(job_id: uuid.UUID, idx: int, *, read: int = 0, written: int = 0, rejected: int = 0, resume: Any = None,
          **fields: Any) -> None:
    values: dict[str, Any] = dict(fields)
    if read:
        values["rows_read"] = JobSplit.rows_read + read
    if written:
        values["rows_written"] = JobSplit.rows_written + written
    if rejected:
        values["rows_rejected"] = JobSplit.rows_rejected + rejected
    if resume is not None:
        values["resume"] = enc(resume)
    if not values:
        return
    with get_engine().begin() as c:
        c.execute(update(JobSplit).where(JobSplit.job_id == job_id, JobSplit.idx == idx).values(**values))


def _totals(job_id: uuid.UUID) -> tuple[int, int, int, int]:
    """(rows read, written, rejected, splits done) summed over the job's splits."""
    with get_engine().connect() as c:
        r = c.execute(select(func.coalesce(func.sum(JobSplit.rows_read), 0), func.coalesce(func.sum(JobSplit.rows_written), 0),
                             func.coalesce(func.sum(JobSplit.rows_rejected), 0)).where(JobSplit.job_id == job_id)).one()
        done = c.execute(select(func.count()).where(JobSplit.job_id == job_id, JobSplit.status == "done")).scalar_one()
    return int(r[0]), int(r[1]), int(r[2]), int(done)


def _aware(d: dt.datetime | None) -> dt.datetime | None:
    if d is not None and d.tzinfo is None:
        return d.replace(tzinfo=dt.timezone.utc)
    return d


# ---- running -------------------------------------------------------------------------

@dataclass
class Ctx:
    job_id: uuid.UUID
    prep: Prepared
    stop: threading.Event
    sink: RejectSink
    write_sem: threading.Semaphore
    fatal: str | None = None
    cancelled: bool = False
    wrote_any: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


def _row_at(table: pa.Table, i: int) -> dict[str, Any]:
    return {k: (None if v is None else _jsonable(v) if not isinstance(v, (int, float, str, bool)) else v)
            for k, v in table.slice(i, 1).to_pylist()[0].items()}


def _write_rows(ctx: Ctx, writer, table: pa.Table) -> tuple[int, list[Reject]]:
    """Write a batch; if the target refuses it because of the data, bisect to isolate the offending rows."""
    n = table.num_rows
    try:
        with ctx.write_sem:
            writer.write(table)
        ctx.wrote_any = True
        return n, []
    except PartialWrite as pw:
        ctx.wrote_any = ctx.wrote_any or len(pw.failed) < n
        return n - len(pw.failed), [(reason, _row_at(table, i)) for i, reason in sorted(pw.failed.items())]
    except Exception as e:  # noqa: BLE001
        if n == 0 or not is_row_error(e):
            raise
        if n == 1:
            return 0, [(short_error(e), _row_at(table, 0))]
        mid = n // 2
        w1, r1 = _write_rows(ctx, writer, table.slice(0, mid))
        w2, r2 = _write_rows(ctx, writer, table.slice(mid))
        rejects = r1 + r2
        if not ctx.wrote_any and len(rejects) >= SYSTEMATIC_REJECTS:
            raise SystematicError(f"Every row was refused by the target, so this is not bad data. First error: {rejects[0][0]}") from e
        return w1 + w2, rejects


def _process_split(ctx: Ctx, idx: int, spec: SplitSpec, resume: Any) -> None:
    prep, o, t = ctx.prep, ctx.prep.opts, ctx.prep.spec.target
    sp = dataclasses.replace(spec, extra=dict(spec.extra))
    if resume is not None and spec.resumable:
        if spec.kind == "file":  # a fixed file: skip exactly the rows already committed
            sp.extra["offset"] = resume
        else:
            sp.extra["resume_after" if prep.unique_split else "resume_from"] = resume
    reject_prefix = prep.src.reject_prefix(spec) if hasattr(prep.src, "reject_prefix") else ""
    reader = Prefetcher(prep.src.read_batches(prep.req, sp, o.read_chunk), ctx.stop)
    writer = prep.tgt.open_writer(t.table, prep.target_cols, t.mode, t.keys)
    pending: list[pa.Table] = []
    pending_rows = pending_read = pending_rej = 0
    last_key: Any = None

    def flush() -> None:
        nonlocal pending, pending_rows, pending_read, pending_rej, last_key
        table = pa.concat_tables(pending) if len(pending) > 1 else (pending[0] if pending else None)
        written, rejects = _write_rows(ctx, writer, table) if table is not None and table.num_rows else (0, [])
        for reason, row in rejects:
            ctx.sink.add(idx, reject_prefix + reason, row)
        _bump(ctx.job_id, idx, read=pending_read, written=written, rejected=pending_rej + len(rejects), resume=last_key)
        if ctx.sink.exceeded:
            raise TooManyRejects(f"More than {o.max_rejects} rows were rejected (see the rejects file); stopping.")
        pending, pending_rows, pending_read, pending_rej = [], 0, 0, 0

    try:
        for batch in reader:
            if ctx.stop.is_set():
                raise Cancelled()
            for reason, row in batch.rejects:
                ctx.sink.add(idx, reason, row)
            b_table = apply_steps(prep.steps, batch.table) if prep.steps else batch.table
            table, rej = prep.tf.apply(b_table)
            for reason, row in rej:
                ctx.sink.add(idx, reject_prefix + reason, row)
            pending_read += batch.table.num_rows + len(batch.rejects)
            pending_rej += len(batch.rejects) + len(rej)
            if table.num_rows:
                pending.append(table)
                pending_rows += table.num_rows
            if batch.resume is not None:
                last_key = batch.resume
            if pending_rows >= o.write_batch or pending_read >= o.write_batch:
                flush()
        if pending or pending_read:
            flush()
    finally:
        reader.close()
        writer.close()


def _run_split(ctx: Ctx, idx: int) -> None:
    prep, o = ctx.prep, ctx.prep.opts
    with get_sessionmaker()() as s:
        row = s.get(JobSplit, (ctx.job_id, idx))
        spec, resume, rows_before, attempts = SplitSpec.from_json(row.spec), dec(row.resume), row.rows_written, row.attempts
    tag = f"split {idx + 1}"
    _bump(ctx.job_id, idx, status="running", started_at=utcnow(), error=None)
    joblog.push(ctx.job_id, "info", f"{tag} started: {spec.label}")
    while True:
        attempts += 1
        _bump(ctx.job_id, idx, attempts=attempts)
        try:
            _process_split(ctx, idx, spec, resume)
            _bump(ctx.job_id, idx, status="done", finished_at=utcnow())
            joblog.push(ctx.job_id, "info", f"{tag} finished")
            rc = prep.remote
            if rc is not None and rc.record:  # its rows are committed: a later run with skip-loaded leaves it out
                from ..remote import record_load
                record_load(rc.skey, rc.tkey, rc.records[spec.extra.get("file", idx)], ctx.job_id)
            return
        except Cancelled:
            _bump(ctx.job_id, idx, status="cancelled", finished_at=utcnow())
            return
        except Exception as e:  # noqa: BLE001
            if ctx.stop.is_set():
                _bump(ctx.job_id, idx, status="cancelled", finished_at=utcnow())
                return
            with get_sessionmaker()() as s:
                cur = s.get(JobSplit, (ctx.job_id, idx))
                resume, rows_before = dec(cur.resume), cur.rows_written
            can_resume = spec.resumable and resume is not None and (
                spec.kind == "file" or prep.unique_split or prep.spec.target.mode == "upsert")
            safe = rows_before == 0 or prep.spec.target.mode == "upsert" or can_resume
            if is_transient(e) and attempts <= o.retries and safe:
                wait_s = min(2 ** attempts, 20)
                joblog.push(ctx.job_id, "warn", f"{tag} failed ({short_error(e)}); retry {attempts}/{o.retries} in {wait_s}s"
                            + (" from the last committed row" if can_resume else ""))
                if ctx.stop.wait(wait_s):
                    _bump(ctx.job_id, idx, status="cancelled", finished_at=utcnow())
                    return
                continue
            why = short_error(e)
            if is_transient(e) and not safe:
                why += " (not retried: rows were already written and the split cannot resume exactly, a retry would duplicate them)"
            _bump(ctx.job_id, idx, status="failed", error=why[:1000], finished_at=utcnow())
            joblog.push(ctx.job_id, "error", f"{tag} failed: {why}")
            with ctx.lock:
                ctx.fatal = ctx.fatal or f"{tag} ({spec.label}): {why}"
            ctx.stop.set()  # the rest of the job winds down; finished splits stay, the job can be re-run to continue
            return


def _finish(job_id: uuid.UUID, status: str, error: str | None, ctx: Ctx | None, rate: float | None) -> None:
    r, w, rej, _ = _totals(job_id)
    with get_sessionmaker()() as s:
        job = s.get(Job, job_id)
        job.status, job.error = status, error
        job.rows_read, job.rows_written, job.rows_rejected = r, w, rej
        job.finished_at, job.heartbeat_at = utcnow(), utcnow()
        if rate is not None:
            job.rate_rows_s = rate
        if status == "succeeded" and ctx and job.spec.get("options", {}).get("strategy") == "watermark":
            _save_watermark(s, job, ctx)
        if status == "succeeded" and ctx and hasattr(ctx.prep.src, "finish"):
            ctx.prep.src.finish(s, job)  # e.g. a combined source's new watermark
        if (job.spec.get("source") or {}).get("combined"):
            job.phase = status
        s.commit()
    if ctx:
        ctx.sink.close()
        if hasattr(ctx.prep.src, "close"):
            ctx.prep.src.close()  # e.g. a stream-mode combined source's in-memory lookups
    if status == "succeeded":  # downloads / scratch files are only kept for a Retry of a failed / cancelled job
        from ..remote import remove_job_dir
        from .combined_run import remove_job_dir as remove_combined_dir
        remove_job_dir(job_id)
        remove_combined_dir(job_id)
    prune_finished()


def _same_pipeline_running(s: Session, job: Job) -> Job | None:
    """Another queued / running job of the same pipeline (a running one always wins; else the older one)."""
    for other in s.scalars(select(Job).where(Job.pipeline_id == job.pipeline_id, Job.id != job.id,
                                             Job.status.in_(("queued", "planning", "running")))):
        hb = _aware(other.heartbeat_at)
        fresh = hb is not None and (utcnow() - hb).total_seconds() < 90
        if other.status in ("running", "planning") and fresh:
            return other
        if other.status == "queued" and (other.created_at, str(other.id)) < (job.created_at, str(job.id)):
            return other
    return None


def _save_watermark(s: Session, job: Job, ctx: Ctx) -> None:
    keys = [dec(sp.resume) for sp in job.splits if sp.resume]
    if not keys or not ctx.prep.req.split_col:
        return
    top = max(keys)
    wm = s.scalar(select(Watermark).where(Watermark.source_key == ctx.prep.src_key, Watermark.target_key == ctx.prep.tgt_key,
                                          Watermark.column == ctx.prep.req.split_col))
    if wm is None:
        s.add(Watermark(source_key=ctx.prep.src_key, target_key=ctx.prep.tgt_key, column=ctx.prep.req.split_col, value=enc(top)))
    else:
        wm.value = enc(top)
    joblog.push(job.id, "info", f"saved watermark {ctx.prep.req.split_col} = {top}")


class _Beat:
    """Keeps a job's heartbeat fresh while a slow step runs (planning can take a while on a big source), so a task the
    queue redelivers meanwhile sees a live job instead of starting a second run."""

    def __init__(self, jid: uuid.UUID) -> None:
        self.jid, self.stop = jid, threading.Event()
        self.t = threading.Thread(target=self._loop, daemon=True, name=f"beat-{str(jid)[:8]}")

    def _loop(self) -> None:
        while not self.stop.wait(10.0):
            try:
                with get_sessionmaker()() as s:
                    s.execute(update(Job).where(Job.id == self.jid).values(heartbeat_at=utcnow()))
                    s.commit()
            except Exception:  # noqa: BLE001 - best effort; the next tick tries again
                log.debug("heartbeat failed", exc_info=True)

    def __enter__(self) -> "_Beat":
        self.t.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop.set()
        self.t.join(timeout=5)


def run_job(job_id: uuid.UUID | str) -> str:
    """Run (or resume) a job to completion in the calling thread. Returns the final status."""
    status = _run_job(job_id)
    if status in ("succeeded", "failed"):
        from ..alerts.engine import job_finished

        job_finished(job_id, status)  # never raises: alerts can't fail a job
    return status


def _run_job(job_id: uuid.UUID | str) -> str:
    jid = uuid.UUID(str(job_id))
    Session = get_sessionmaker()
    with Session() as s:
        job = s.get(Job, jid)
        if job is None:
            return "missing"
        if job.status in ("succeeded", "cancelled"):
            return job.status
        if job.cancel_requested:
            job.status, job.finished_at = "cancelled", utcnow()
            s.commit()
            return "cancelled"
        hb = _aware(job.heartbeat_at)
        if job.status in ("planning", "running") and hb and (utcnow() - hb).total_seconds() < 45:
            return "already-running"  # a redelivered task while the first copy is still alive
        combined_src = (job.spec.get("source") or {}).get("combined")
        # A combined source stages its inputs while "preparing", which can take hours: show it as running (with a
        # phase), so a worker that dies meanwhile turns the job stalled - and retryable - instead of stuck planning.
        job.status, job.error, job.heartbeat_at = ("running" if combined_src else "planning"), None, utcnow()
        job.phase = "staging" if combined_src and not job.splits else job.phase
        job.started_at = job.started_at or utcnow()
        job.finished_at = None
        s.commit()
        first_run = not job.splits
        try:
            with _Beat(jid):
                spec = JobIn.model_validate(job.spec)
                if spec.source.remote is not None:
                    from ..remote import overlapping_job

                    if other := overlapping_job(s, job.spec, job):
                        raise JobError(f"Another job ({other.name}, {other.status}) is loading from this source into this "
                                       "target: two loads with 'skip files already loaded' would load the same new files twice.")
                if spec.source.combined is not None and spec.source.combined.watermark and job.pipeline_id:
                    if other := _same_pipeline_running(s, job):
                        raise JobError(f"Another run of this pipeline ({other.status}) is still going: two runs with a "
                                       "watermark would read the same new rows twice.")
                prep = prepare(spec, s, job_id=jid)
                if first_run:
                    joblog.push(jid, "info", "planning")
                    lay = layout(prep, execute_target=True)
                    job.plan = {
                        "source_columns": [c.to_dict() for c in prep.req.columns],
                        "target_columns": [{**o.target.to_dict(), "source_name": o.source} for o in prep.out],
                        "target_exists": prep.target_exists, "ddl": lay.ddl, "split_column": prep.req.split_col,
                        "notes": lay.notes, "warnings": lay.warnings,
                    }
                    job.rows_total, job.rows_total_exact, job.splits_total, job.workers = lay.total, lay.exact, len(lay.splits), lay.workers
                    for sp in lay.splits:
                        s.add(JobSplit(job_id=jid, idx=sp.idx, label=sp.label, spec=sp.to_json(), est_rows=sp.est_rows))
                    for line in lay.ddl:
                        joblog.push(jid, "info", "target: " + line.splitlines()[0][:120])
                    joblog.push(jid, "info", f"{len(lay.splits)} splits on {prep.req.split_col or 'the whole source'}, {lay.workers} workers")
                    s.commit()
        except Exception as e:  # noqa: BLE001
            from ..remote import NothingToLoad
            from .staging import StagingCancelled

            if isinstance(e, StagingCancelled):  # cancelled while staging a combined source
                job.status, job.finished_at, job.phase = "cancelled", utcnow(), "cancelled while staging"
                s.commit()
                joblog.push(jid, "warn", "job cancelled while staging")
                prune_finished()
                return "cancelled"
            if isinstance(e, NothingToLoad):  # every matching remote file is already loaded: a 0-row success
                job.status, job.finished_at, job.splits_total = "succeeded", utcnow(), 0
                job.rows_read = job.rows_written = job.rows_rejected = 0
                job.plan = {**(job.plan or {}), "notes": [str(e)], "warnings": []}
                s.commit()
                joblog.push(jid, "info", f"nothing to load: {e}")
                prune_finished()
                return "succeeded"
            msg = short_error(e)
            job.status, job.error, job.finished_at = "failed", msg[:1000], utcnow()
            s.commit()
            joblog.push(jid, "error", f"could not start: {msg}")
            prune_finished()
            return "failed"
        s.refresh(job)
        workers = max(1, min(job.workers or 1, prep.opts.workers))
        todo = [sp.idx for sp in job.splits if sp.status != "done"]
        job.status, job.heartbeat_at = "running", utcnow()
        s.commit()
        max_rejects = spec.options.max_rejects

    ctx = Ctx(jid, prep, threading.Event(), RejectSink(rejects_path(jid), max_rejects), threading.Semaphore(max(1, prep.tgt.max_writers)))
    if not first_run:
        joblog.push(jid, "info", f"continuing: {len(todo)} splits left")
    for i in todo:
        _bump(jid, i, status="pending")
    samples: deque[tuple[float, int]] = deque([(time.monotonic(), _totals(jid)[1])])  # (time, rows written) over the last 30 s
    ema: float | None = None  # kept under its old name: the smoothed rows/s
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="conduit-split") as pool:
        futures = [pool.submit(_run_split, ctx, i) for i in todo]
        pending = set(futures)
        while pending:
            done, pending = wait(pending, timeout=1.0)
            r, w, rej, _ = _totals(jid)
            now = time.monotonic()
            samples.append((now, w))
            while len(samples) > 2 and now - samples[1][0] >= RATE_WINDOW_S:
                samples.popleft()  # window = the last ~30 s, or the whole run while it is younger than that
            t0, w0 = samples[0]
            if now - t0 >= 1.5:
                ema = (w - w0) / (now - t0)
            with Session() as s2:
                j2 = s2.get(Job, jid)
                if j2.cancel_requested and not ctx.stop.is_set():
                    ctx.cancelled = True
                    ctx.stop.set()
                    joblog.push(jid, "warn", "cancel requested: stopping after the current batches")
                j2.rows_read, j2.rows_written, j2.rows_rejected = r, w, rej
                j2.rate_rows_s, j2.heartbeat_at = ema, utcnow()
                s2.commit()
            for f in done:
                if f.exception():  # bugs, not job failures (those are recorded per split)
                    with ctx.lock:
                        ctx.fatal = ctx.fatal or short_error(f.exception())
                    ctx.stop.set()

    if ctx.cancelled:
        _finish(jid, "cancelled", None, ctx, ema)
        joblog.push(jid, "warn", "job cancelled")
        return "cancelled"
    if ctx.fatal:
        _finish(jid, "failed", ctx.fatal, ctx, ema)
        return "failed"
    r, w, rej, done_n = _totals(jid)
    _finish(jid, "succeeded", None, ctx, ema)
    joblog.push(jid, "info", f"finished: {w:,} rows written, {rej:,} rejected")
    return "succeeded"


# ---- housekeeping and reading state ---------------------------------------------------------

def prune_finished() -> None:
    """Keep only the latest few finished jobs (no history)."""
    keep = current_settings().keep_finished_jobs
    with get_sessionmaker()() as s:
        old = list(s.scalars(select(Job).where(Job.status.in_(("succeeded", "failed", "cancelled")))
                             .order_by(Job.finished_at.desc().nullslast(), Job.created_at.desc()).offset(keep)))
        from ..remote import remove_job_dir
        from .combined_run import remove_job_dir as remove_combined_dir

        for j in old:
            rejects_path(j.id).unlink(missing_ok=True)
            remove_job_dir(j.id)
            remove_combined_dir(j.id)
            joblog.clear(j.id)
            s.execute(delete(JobSplit).where(JobSplit.job_id == j.id))
            s.delete(j)
        s.commit()


def snapshot(job: Job, splits: list[JobSplit] | None = None) -> dict[str, Any]:
    """Current state of a job as the UI shows it: counters, progress fraction, rate and ETA."""
    active = job.status in ("planning", "running")
    if active or job.status == "queued":  # a queued job may be a retry: show what its splits already committed
        r, w, rej, done = _totals(job.id)
    else:
        r, w, rej = job.rows_read, job.rows_written, job.rows_rejected
        done = sum(1 for sp in (splits if splits is not None else job.splits) if sp.status == "done")
    processed = w + rej
    total = job.rows_total
    exact = job.rows_total_exact
    if total is None or processed > total:
        total, exact = (max(total or 0, processed) or None), False
    frac = None
    if total:
        frac = 1.0 if job.status == "succeeded" else min(processed / total, 0.99 if active else 1.0)
    rate = job.rate_rows_s if active else None
    eta = None
    if active and rate and rate > 1 and total and job.status == "running":
        eta = max(int((total - processed) / rate), 0)
    hb = _aware(job.heartbeat_at)
    stalled = bool(job.status == "running" and hb and (utcnow() - hb).total_seconds() > 90)
    return {
        "id": str(job.id), "name": job.name, "kind": job.kind, "status": "stalled" if stalled else job.status,
        "rows_total": total, "rows_total_exact": exact, "rows_read": r, "rows_written": w, "rows_rejected": rej,
        "progress": frac, "rate_rows_s": round(rate) if rate else None, "eta_s": eta, "workers": job.workers,
        "splits_total": job.splits_total, "splits_done": done, "error": job.error,
        "created_at": job.created_at, "started_at": job.started_at, "finished_at": job.finished_at,
        "has_rejects": rejects_path(job.id).exists() and rejects_path(job.id).stat().st_size > 0,
        "phase": job.phase,  # a combined source: staging … / joining / loading
    }
