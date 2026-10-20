"""Staging: copy one or more sources into tables of a scratch DuckDB file, in parallel, through the normal adapters.

Shared by Compare (its two sides) and combined pipelines (every input). Each input is read with the load
engine's adapters (a database table / query, a local or remote file), cast to the logical type vocabulary
(engine/types.py) so DuckDB sees sensible, comparable types - time-zone-aware times become naive UTC - and
appended to its own table. Every table is created before the readers start, so the threads only ever append
(DuckDB handles concurrent appends to different tables from separate cursors).

The caller passes a `tick` callback (called about once a second with the rows read so far per table; return True
to cancel) and an optional `guard` (called between batches, e.g. a free-disk-space check that raises).
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import duckdb
import pyarrow as pa

from .model import ReadRequest
from .spec import SourceIn
from .types import ColSpec, to_arrow


class StagingCancelled(Exception):
    pass


class StagingError(Exception):
    """Reading one input failed; the message names it."""


class _GuardStop(Exception):
    """Carries what the guard raised past the generic "reading failed" wrapping."""

    def __init__(self, error: BaseException) -> None:
        super().__init__(str(error))
        self.error = error


def qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass
class StageInput:
    table: str  # DuckDB table to create (any name; quoted here)
    adapter: Any  # an engine adapter, FileAdapter or MultiFileAdapter
    source: SourceIn  # where to read: table / query / where
    columns: list[ColSpec]  # columns to read, by source name, with their logical types
    names: list[str] | None = None  # stored column names (default: the source names)
    label: str = ""  # how messages name the input
    request: ReadRequest | None = None  # a prepared read (e.g. "rows above the watermark"); default: the whole source
    limit: int | None = None  # stop after this many rows (previews)

    @property
    def stored_names(self) -> list[str]:
        return self.names or [c.name for c in self.columns]


@dataclass
class StageResult:
    rows: dict[str, int] = field(default_factory=dict)  # table -> rows staged
    capped: set[str] = field(default_factory=set)  # tables that stopped at their row limit


def storage_schema(cols: list[ColSpec], names: list[str]) -> pa.Schema:
    """Arrow schema an input is stored with: logical types, time-zone-aware times as naive UTC."""
    fields = []
    for c, n in zip(cols, names):
        t = to_arrow(c.logical)
        if pa.types.is_timestamp(t) and t.tz:
            t = pa.timestamp(t.unit)
        fields.append(pa.field(n, t))
    return pa.schema(fields)


def to_storage(table: pa.Table, cols: list[ColSpec], schema: pa.Schema) -> pa.Table:
    arrays = []
    for c in cols:
        arr = table.column(c.name)
        if pa.types.is_timestamp(arr.type) and arr.type.tz:
            arr = arr.cast(pa.timestamp(arr.type.unit))  # the same instant, as UTC wall-clock time
        arrays.append(arr)
    t = pa.Table.from_arrays(arrays, names=schema.names)
    try:
        return t.cast(schema, safe=False)
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError):
        return t  # let DuckDB cast on insert


def create_table(con: duckdb.DuckDBPyConnection, inp: StageInput) -> None:
    con.register("empty_input", storage_schema(inp.columns, inp.stored_names).empty_table())
    con.execute(f"CREATE TABLE {qi(inp.table)} AS SELECT * FROM empty_input")
    con.unregister("empty_input")


def read_request(inp: StageInput) -> ReadRequest:
    if inp.request is not None:
        inp.request.columns = inp.columns
        return inp.request
    s = inp.source
    return ReadRequest(table=s.table, query=s.query, where=s.where, columns=inp.columns, mode="full")


def _read(inp: StageInput, con: duckdb.DuckDBPyConnection, counter: list[int], stop: threading.Event, chunk: int,
          guard: Callable[[StageInput], None] | None, capped: set[str]) -> None:
    schema = storage_schema(inp.columns, inp.stored_names)
    req = read_request(inp)
    for split in inp.adapter.plan_splits(req, 1):
        gen = inp.adapter.read_batches(req, split, min(chunk, inp.limit) if inp.limit else chunk)
        try:
            for batch in gen:
                if stop.is_set():
                    raise StagingCancelled()
                if batch.table.num_rows == 0:
                    continue
                part = to_storage(batch.table, inp.columns, schema)
                if inp.limit is not None and counter[0] + part.num_rows >= inp.limit:
                    part = part.slice(0, inp.limit - counter[0])
                    capped.add(inp.table)
                con.register("batch", part)
                con.execute(f"INSERT INTO {qi(inp.table)} SELECT * FROM batch")
                con.unregister("batch")
                counter[0] += part.num_rows
                if guard is not None:
                    try:
                        guard(inp)
                    except BaseException as e:  # noqa: BLE001
                        raise _GuardStop(e) from e
                if inp.table in capped:
                    return
        finally:
            gen.close()


def stage(con: duckdb.DuckDBPyConnection, inputs: list[StageInput], *, chunk: int = 50_000,
          tick: Callable[[dict[str, int]], bool] | None = None, guard: Callable[[StageInput], None] | None = None,
          poll_s: float = 1.0) -> StageResult:
    """Stage every input into its own table, in parallel. Raises StagingError naming the input that failed,
    StagingCancelled when `tick` asked to stop, or whatever `guard` raised."""
    for inp in inputs:
        create_table(con, inp)
    stop = threading.Event()
    counts = {inp.table: [0] for inp in inputs}
    capped: set[str] = set()
    errors: list[BaseException] = []

    def reader(inp: StageInput) -> None:
        cur = con.cursor()
        try:
            _read(inp, cur, counts[inp.table], stop, chunk, guard, capped)
        except StagingCancelled:
            pass
        except _GuardStop as g:  # e.g. the disk filled up: as the guard said it
            errors.append(g.error)
            stop.set()
        except BaseException as e:  # noqa: BLE001 - surfaced by the calling thread
            from .runner import short_error

            errors.append(StagingError(f"Reading {inp.label or inp.table} failed: {short_error(e)}"))
            stop.set()
        finally:
            cur.close()

    threads = [threading.Thread(target=reader, args=(inp,), daemon=True, name=f"stage-{inp.table}"[:30]) for inp in inputs]
    for th in threads:
        th.start()
    while any(th.is_alive() for th in threads):
        for th in threads:
            th.join(timeout=poll_s / max(len(threads), 1))
        if tick is not None and tick({k: v[0] for k, v in counts.items()}):
            stop.set()
    if errors:
        raise errors[0]
    if stop.is_set():
        raise StagingCancelled()
    if tick is not None and tick({k: v[0] for k, v in counts.items()}):
        raise StagingCancelled()
    return StageResult({k: v[0] for k, v in counts.items()}, capped)
