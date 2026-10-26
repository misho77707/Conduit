"""Stream mode of a combined pipeline source: one big driver input streamed, small lookups held in memory.

The lookups (every input but the driver) are read once per job into an in-process DuckDB, each capped in rows and
the whole in memory (combined_lookup_max_rows / _mb, a runtime setting): a bigger lookup fails the job early and points to
staged mode. The driver is never copied: each of its batches is registered under the driver's alias and the SQL
runs over it, so the SQL must keep each driver row's result independent of other batches (checked cheaply when
the pipeline is saved: no GROUP BY / DISTINCT / window / aggregate / LIMIT at the top level).

A Retry restarts the driver from the beginning (from the saved watermark, when there is one): rows a failed
attempt already wrote are written again, so pair stream mode with upsert, or use staged mode, when that matters.
"""
from __future__ import annotations

from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
from sqlalchemy.orm import Session

from ..runtime import current_settings
from ..models import Job
from . import joblog
from .arrowconv import conform
from .combined import CombinedError, open_input, result_columns
from .combined_run import (_minus_overlap, _progress, _watermark_column, driver_request, estimate, job_dir,
                           save_watermark, saved_watermark)
from .filesource import FileAdapter, MultiFileAdapter
from .model import Batch, ReadRequest, SplitSpec
from .spec import CombinedIn, JobIn
from .staging import StageInput, StagingError, qi, read_request, stage, storage_schema, to_storage
from .types import ColSpec

BATCH = "__conduit_batch"


def _memory_mb(con: duckdb.DuckDBPyConnection) -> float:
    cur = con.cursor()
    try:
        return float(cur.execute("SELECT coalesce(sum(memory_usage_bytes), 0) FROM duckdb_memory()").fetchone()[0]) / 1024 ** 2
    finally:
        cur.close()


def stream_source(session: Session, spec: JobIn, job: Job) -> "StreamSource":
    c = spec.source.combined
    st = current_settings()
    d = job_dir(job.id)
    lookups, driver = [], None
    for inp in c.inputs:
        src, cols = open_input(session, inp, full_dir=d / "remote" / inp.alias)
        if inp.alias == c.driver:
            driver = (inp, src, cols)
        else:
            lookups.append(StageInput(inp.alias, src, inp.source, cols, label=f"lookup {inp.alias}",
                                      limit=st.combined_lookup_max_rows + 1))
    inp, dsrc, dcols = driver
    lo = wm_col = None
    if c.watermark:
        prev = saved_watermark(session, job, c)
        lo = _minus_overlap(prev, c.watermark.overlap)
        wm_col = _watermark_column(c, dcols)
        joblog.push(job.id, "info", f"watermark: reading {c.driver} rows with {c.watermark.column} > {lo}" if prev is not None
                    else f"watermark: first run, reading all of {c.driver}")

    con = duckdb.connect()  # in memory: the lookups, and each driver batch while the SQL runs over it

    def guard(si: StageInput) -> None:
        if _memory_mb(con) > st.combined_lookup_max_mb:
            raise CombinedError(f"The lookups take more than {st.combined_lookup_max_mb:,} MB of memory while reading "
                                f"{si.table} (Settings: Stream-mode lookup memory): use staged mode for big inputs")

    est = {si.table: estimate(si) for si in lookups}
    try:
        res = stage(con, lookups, chunk=spec.options.read_chunk, guard=guard,
                    tick=lambda n: _progress(job.id, "loading lookups " + " · ".join(
                        f"{a} {v:,}{f' / ~{est[a]:,}' if est.get(a) else ''}" for a, v in n.items())))
    except StagingError as e:
        con.close()
        raise CombinedError(str(e)) from e
    except BaseException:
        con.close()
        raise
    for alias in res.capped:
        con.close()
        raise CombinedError(f"Lookup {alias} has more than {st.combined_lookup_max_rows:,} rows "
                            "(Settings: Stream-mode lookup rows): use staged mode, or narrow it with a WHERE / column subset")
    for alias, n in res.rows.items():
        joblog.push(job.id, "info", f"lookup {alias}: {n:,} rows in memory")
    req = driver_request(c, dsrc, inp.source, wm_col, lo) if c.watermark else None
    s = StreamSource(con, c, dsrc, inp.source, dcols, req, lo, wm_col, res.rows)
    _progress(job.id, f"loading (streaming {c.driver})")
    return s


class StreamSource:
    """The driver streamed batch by batch, the SQL run over each batch and the in-memory lookups."""

    dialect = "combined"
    max_writers = 0

    def __init__(self, con, c: CombinedIn, driver: Any, source, cols: list[ColSpec], req: ReadRequest | None,
                 lo: Any, wm_col: ColSpec | None, lookup_rows: dict[str, int]) -> None:
        self.con, self.c, self.driver, self.source, self.cols = con, c, driver, source, cols
        self.req, self.lo, self.wm_col = req, lo, wm_col
        self.file_filter = lo is not None and isinstance(driver, (FileAdapter, MultiFileAdapter))
        self.schema = storage_schema(cols, [x.name for x in cols])
        self.max_wm: Any = None
        # the SQL's exact output, from an empty but correctly typed driver
        con.register(BATCH, self.schema.empty_table())
        con.execute(f"CREATE OR REPLACE TEMP VIEW {qi(c.driver)} AS SELECT * FROM {BATCH}")
        try:
            self.columns = result_columns(con.sql(c.sql))
        except duckdb.Error as e:
            raise CombinedError(f"The SQL failed on the inputs: {' '.join(str(e).split())}") from e
        finally:
            con.unregister(BATCH)
        lk = ", ".join(f"{a} {n:,} rows" for a, n in lookup_rows.items())
        self.notes = [f"Combined (stream): {c.driver} streamed; lookups in memory: {lk}.",
                      "A Retry of a stream-mode run restarts the driver: use upsert (or staged mode) to avoid duplicates."]

    def read_columns(self) -> list[ColSpec]:
        return list(self.columns)

    def suggest_columns(self) -> list[ColSpec]:
        return list(self.columns)

    def estimate_rows(self, req: ReadRequest | None = None, exact: bool = False) -> tuple[int | None, bool]:
        return estimate(StageInput(self.c.driver, self.driver, self.source, self.cols, request=self.req)), False

    def key_range(self, req: ReadRequest) -> None:
        return None

    def is_splittable(self, logical: str | None) -> bool:
        return False

    def plan_splits(self, req: ReadRequest, n: int) -> list[SplitSpec]:
        return [SplitSpec(0, f"{self.c.driver} (streamed)", "full")]

    def read_batches(self, req: ReadRequest, split: SplitSpec, batch_rows: int):
        drv_req = read_request(StageInput(self.c.driver, self.driver, self.source, self.cols, request=self.req))
        alias = qi(self.c.driver)
        wanted = [x.name for x in req.columns]
        for dsplit in self.driver.plan_splits(drv_req, 1):
            gen = self.driver.read_batches(drv_req, dsplit, batch_rows)
            try:
                for batch in gen:
                    if batch.table.num_rows == 0:
                        continue
                    part = to_storage(batch.table, self.cols, self.schema)
                    if self.file_filter:  # a file driver: its rows above the watermark are picked here
                        col = part.column(self.wm_col.name)
                        part = part.filter(pc.fill_null(pc.greater(col, pa.scalar(self.lo, type=col.type)), False))
                        if part.num_rows == 0:
                            continue
                    self.con.register(BATCH, part)
                    try:
                        self.con.execute(f"CREATE OR REPLACE TEMP VIEW {alias} AS SELECT * FROM {BATCH}")
                        if self.wm_col is not None:
                            top = self.con.execute(f"SELECT max({qi(self.wm_col.name)}) FROM {alias}").fetchone()[0]
                            if top is not None and (self.max_wm is None or top > self.max_wm):
                                self.max_wm = top
                        out = self.con.execute(self.c.sql).fetch_arrow_table()
                    except duckdb.Error as e:
                        raise CombinedError(f"The SQL failed on a batch of {self.c.driver}: {' '.join(str(e).split())}") from e
                    finally:
                        self.con.unregister(BATCH)
                    if out.num_rows:
                        yield Batch(conform(out.select(wanted), req.columns))
            finally:
                gen.close()

    def finish(self, session: Session, job: Job) -> None:
        if self.c.watermark and self.max_wm is not None:
            save_watermark(session, job, self.c, self.max_wm)

    def close(self) -> None:
        self.con.close()
