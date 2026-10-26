"""Combined sources: several inputs (tables, queries, files) joined by one DuckDB SELECT over their aliases.

Pipelines only. Two execution modes:
  - staged (default): every input is copied into a scratch DuckDB file (engine/staging.py), the SQL runs once over
    the full data and its result is loaded. Any SQL works, including whole-table GROUP BY / DISTINCT / windows.
  - stream: one input (the driver) is streamed in batches and never copied; every other input is a lookup, read
    once per job into an in-process DuckDB (size-capped). The SQL runs once per driver batch, so - like a pipeline
    `sql` step - it may not aggregate across batches.

This module holds the offline checks (no database is touched: DuckDB's own parser, in memory), the preview (inputs
staged with a row cap, the SQL run over the sample) and the planned source `prepare()` uses at launch, which
only knows the cached output schema (staging happens inside the job).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import duckdb
import pyarrow as pa
from sqlalchemy.orm import Session

from ..runtime import current_settings
from ..connectors.base import UserFacingError
from .filesource import MultiFileAdapter
from .model import Batch, ReadRequest, SplitSpec
from .spec import CombinedIn, CombinedInputIn, StepOutCol
from .staging import StageInput, StagingError, qi, stage
from .types import ColSpec, normalize


class CombinedError(UserFacingError, ValueError):
    """The combined source is not valid / cannot run as given; the message is written for people."""


# ---- offline checks ----------------------------------------------------------------------------

def _parse(sql: str) -> dict[str, Any]:
    con = duckdb.connect()
    try:
        ast = json.loads(con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0])
    finally:
        con.close()
    if ast.get("error"):
        msg = str(ast.get("error_message") or "")
        if "Only SELECT" in msg:
            raise CombinedError("The SQL must be one SELECT (or WITH … SELECT) statement")
        raise CombinedError(f"The SQL does not parse: {msg}")
    if len(ast.get("statements") or []) != 1:
        raise CombinedError("The SQL must be exactly one SELECT statement (no ';'-separated statements)")
    return ast["statements"][0]["node"]


_AGGREGATES: set[str] | None = None


def _aggregates() -> set[str]:
    global _AGGREGATES
    if _AGGREGATES is None:
        con = duckdb.connect()
        try:
            _AGGREGATES = {r[0].lower() for r in con.execute(
                "SELECT DISTINCT function_name FROM duckdb_functions() WHERE function_type = 'aggregate'").fetchall()}
        finally:
            con.close()
    return _AGGREGATES


def _walk(node: Any, skip_subqueries: bool = True):
    """Every expression dict in `node` (not descending into subqueries)."""
    if isinstance(node, dict):
        yield node
        for k, v in node.items():
            if skip_subqueries and k == "subquery":
                continue
            yield from _walk(v, skip_subqueries)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, skip_subqueries)


def tables_read(sql: str) -> set[str]:
    try:
        return set(duckdb.get_table_names(sql))
    except duckdb.Error as e:
        raise CombinedError(f"The SQL does not parse: {e}") from e


def check(c: CombinedIn) -> list[str]:
    """Validate a combined source without touching any database. Raises CombinedError; returns warnings."""
    node = _parse(c.sql)
    aliases = {i.alias.lower(): i.alias for i in c.inputs}
    read = tables_read(c.sql)
    unknown = sorted(t for t in read if t.split(".")[-1].lower() not in aliases)
    if unknown:
        raise CombinedError(f"The SQL reads {', '.join(unknown)}, which {'is' if len(unknown) == 1 else 'are'} not an input "
                            f"(inputs: {', '.join(i.alias for i in c.inputs)})")
    used = {t.split(".")[-1].lower() for t in read}
    warnings = [f"Input {i.alias} is not used by the SQL" for i in c.inputs if i.alias.lower() not in used]
    if c.mode == "stream":
        if c.driver and c.driver.lower() not in used:
            raise CombinedError(f"The SQL never reads the driver {c.driver}: stream mode runs it once per driver batch")
        why = _not_per_batch(node)
        if why:
            raise CombinedError(f"Stream mode runs the SQL once per batch of {c.driver}, so it can't {why}. "
                                "Use staged mode for this SQL.")
    return warnings


def _not_per_batch(node: dict[str, Any]) -> str | None:
    """Why this SELECT would give different results when run per batch instead of over everything (cheap checks)."""
    if node.get("type") != "SELECT_NODE":
        return "combine several SELECTs (UNION / INTERSECT / EXCEPT)"
    if node.get("group_expressions"):
        return "GROUP BY"
    for m in node.get("modifiers") or []:
        if m.get("type") == "DISTINCT_MODIFIER":
            return "use DISTINCT"
        if m.get("type") in ("LIMIT_MODIFIER", "LIMIT_PERCENT_MODIFIER"):
            return "use LIMIT / OFFSET"
    if node.get("qualify"):
        return "use QUALIFY"
    for e in _walk(node.get("select_list")):
        if e.get("class") == "WINDOW":
            return "use window functions (… OVER (…))"
        if e.get("class") in ("FUNCTION", "AGGREGATE") and str(e.get("function_name", "")).lower() in _aggregates():
            return f"aggregate ({e.get('function_name')}(…)) across rows"
    return None


# ---- join keys: compare the types of both sides of `a.x = b.y` -----------------------------------

def _sql_aliases(node: Any) -> dict[str, str]:
    """Names the SQL uses for its tables (its own aliases, e.g. `events e`) -> the input alias."""
    out: dict[str, str] = {}
    for e in _walk(node, skip_subqueries=False):
        if e.get("type") == "BASE_TABLE" and e.get("table_name"):
            out[(e.get("alias") or e["table_name"]).lower()] = e["table_name"]
    return out


def join_keys(sql: str) -> list[tuple[str, str, str, str]]:
    """(alias, column, alias, column) of every `x.a = y.b` between two different tables in the SQL."""
    node = _parse(sql)
    names = _sql_aliases(node)
    keys = []
    for e in _walk(node, skip_subqueries=False):
        if e.get("class") == "COMPARISON" and e.get("type") == "COMPARE_EQUAL":
            l, r = e.get("left") or {}, e.get("right") or {}
            if l.get("class") == r.get("class") == "COLUMN_REF":
                lc, rc = l.get("column_names") or [], r.get("column_names") or []
                if len(lc) >= 2 and len(rc) >= 2:
                    la, ra = names.get(lc[-2].lower(), lc[-2]), names.get(rc[-2].lower(), rc[-2])
                    if la.lower() != ra.lower():
                        keys.append((la, lc[-1], ra, rc[-1]))
    return keys


def _kind(duck_type: str) -> str:
    from ..compare.engine import _cls

    k = _cls(duck_type)
    return {"int": "number", "dec": "number", "float": "number", "date": "date/time", "ts": "date/time"}.get(k, k)


def key_mismatches(con: duckdb.DuckDBPyConnection, sql: str, inputs: list[CombinedInputIn]) -> list[str]:
    """Join conditions whose two columns hold different kinds of values (a number joined to text, ...)."""
    by_alias = {i.alias.lower(): i.alias for i in inputs}
    types: dict[tuple[str, str], str] = {}
    for alias in by_alias.values():
        for name, typ in con.execute(f"SELECT column_name, column_type FROM (DESCRIBE {qi(alias)})").fetchall():
            types[(alias.lower(), name.lower())] = typ
    out = []
    for la, lcol, ra, rcol in join_keys(sql):
        lt, rt = types.get((la.lower(), lcol.lower())), types.get((ra.lower(), rcol.lower()))
        if lt and rt and _kind(lt) != _kind(rt):
            out.append(f"Join key types differ: {by_alias.get(la.lower(), la)}.{lcol} is {lt}, "
                       f"{by_alias.get(ra.lower(), ra)}.{rcol} is {rt}: cast one side in the SQL, or rows may not match")
    return out


# ---- inputs ------------------------------------------------------------------------------------------

def input_columns(inp: CombinedInputIn, raw: list[ColSpec]) -> list[ColSpec]:
    """The columns an input is read with: all of them, or the chosen subset (pushed down to the source)."""
    if not inp.columns:
        return raw
    by = {c.name: c for c in raw}
    missing = [c for c in inp.columns if c not in by]
    if missing:
        raise CombinedError(f"Input {inp.alias} has no column {', '.join(missing)} "
                            f"(it has: {', '.join(list(by)[:12])})")
    return [by[c] for c in inp.columns]


def open_input(session: Session, inp: CombinedInputIn, full_dir=None) -> tuple[Any, list[ColSpec]]:
    """(adapter, columns to read) of one input. `full_dir`: read a remote input in full (downloaded there)."""
    from .runner import JobError
    from .sources import raw_columns, source_adapter

    role = f"input {inp.alias}"
    try:
        src = source_adapter(session, inp.source, role, full_dir=full_dir)
        return src, input_columns(inp, raw_columns(src, inp.source, role))
    except JobError as e:
        raise CombinedError(f"Input {inp.alias}: {e}") from e


def result_columns(rel: duckdb.DuckDBPyRelation) -> list[ColSpec]:
    names = list(rel.columns)
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise CombinedError(f"The SQL returns more than one column named {', '.join(dupes)}: give them distinct "
                            "names (e.g. u.id AS user_id)")
    return [ColSpec(n, normalize("duckdb", str(t)), True) for n, t in zip(names, rel.types)]


# ---- preview -------------------------------------------------------------------------------------------

@dataclass
class CombinedPreview:
    table: pa.Table  # the join result, at most sample_rows rows
    columns: list[ColSpec]  # the join result's exact schema
    info: dict[str, Any] = field(default_factory=dict)


def preview(session: Session, c: CombinedIn, sample_rows: int) -> CombinedPreview:
    """Stage every input with a row cap (in memory), run the SQL over the sample. The schema is exact even when the
    sample has no matching rows; notes say which inputs were capped."""
    cap = current_settings().combined_preview_rows
    warnings = check(c)
    inputs, labels = [], {}
    for inp in c.inputs:
        src, cols = open_input(session, inp)
        multi = isinstance(src, MultiFileAdapter) and len(src.files) > 1
        inputs.append(StageInput(inp.alias, src, inp.source, cols, label=f"input {inp.alias}", limit=cap,
                                 max_splits=1 if multi else None))
        labels[inp.alias] = multi
    con = duckdb.connect()
    try:
        try:
            res = stage(con, inputs, chunk=min(50_000, cap))
        except StagingError as e:
            raise CombinedError(str(e)) from e
        mismatches = key_mismatches(con, c.sql, c.inputs)
        try:
            rel = con.sql(c.sql)
            cols = result_columns(rel)
            join_rows = int(con.execute(f"SELECT count(*) FROM ({c.sql}) AS conduit_join").fetchone()[0])
            table = con.execute(f"SELECT * FROM ({c.sql}) AS conduit_join LIMIT {int(sample_rows)}").fetch_arrow_table()
        except duckdb.Error as e:
            hint = (" " + " ".join(mismatches)) if mismatches else ""
            raise CombinedError(f"The SQL failed on the inputs: {' '.join(str(e).split())}{hint}") from e
        notes = list(warnings)
        for inp in c.inputs:
            if inp.alias in res.capped:
                notes.append(f"Preview joined the first {cap:,} rows of {inp.alias}: matches may be missing.")
            if labels[inp.alias]:
                notes.append(f"Preview read only the first matching file of {inp.alias}.")
        if c.watermark and c.driver:
            notes.append(f"The preview reads {c.driver} from the start; a run reads only its rows above the saved "
                         f"{c.watermark.column} watermark.")
        if c.mode == "stream":
            notes.append(f"Stream mode runs this SQL once per batch of {c.driver}; the preview ran it once over the sample.")
        info = {
            "mode": c.mode,
            "inputs": [{"alias": i.alias, "rows": res.rows[i.alias], "capped": i.alias in res.capped,
                        "columns": [{"name": col.name, "type": col.logical} for col in s.columns]}
                       for i, s in zip(c.inputs, inputs)],
            "join_rows": join_rows, "notes": notes,
            "key_mismatches": mismatches,  # join conditions over columns of different kinds (number = text, ...)
        }
        return CombinedPreview(table, cols, info)
    finally:
        con.close()


# ---- what prepare() sees at launch ------------------------------------------------------------------

class PlannedCombined:
    """A combined source before its job runs: only the cached output schema (staging happens inside the job)."""

    dialect = "combined"
    max_writers = 0

    def __init__(self, columns: list[StepOutCol]) -> None:
        self.columns = [ColSpec(c.name, c.type, c.nullable) for c in columns]
        self.notes: list[str] = []

    def read_columns(self) -> list[ColSpec]:
        return list(self.columns)

    def suggest_columns(self) -> list[ColSpec]:
        return list(self.columns)

    def estimate_rows(self, req: ReadRequest | None = None, exact: bool = False) -> tuple[int | None, bool]:
        return None, False

    def key_range(self, req: ReadRequest) -> None:
        return None

    def plan_splits(self, req: ReadRequest, n: int) -> list[SplitSpec]:
        return [SplitSpec(0, "combined", "full")]

    def is_splittable(self, logical: str | None) -> bool:
        return False

    def read_batches(self, req: ReadRequest, split: SplitSpec, batch_rows: int):
        raise CombinedError("A combined source is only read inside its job (preview the pipeline to see its rows)")
        yield Batch(pa.table({}))  # pragma: no cover - makes this a generator
