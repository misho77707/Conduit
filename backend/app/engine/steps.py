"""Pipeline transform steps: rename, cast, filter, derive, split, SQL and Python.

Steps run per batch, on the same Arrow batches the rest of the load engine already reads and writes
(see runner.py). Each visual step compiles to one generated SQL statement over a view named `input`;
a `sql` step is the user's own statement (also over `input`); a `python` step instead execs a
user-defined `transform(df)` against a pandas DataFrame. Because steps see one batch at a time, not the
whole source, anything that needs the complete result set at once - GROUP BY, DISTINCT, a window
function over the full table - is not supported: that is a known v1 limitation, not an oversight.

Python steps run with the same privileges as the Celery worker process: there is no sandboxing (no
separate venv, no network/filesystem restriction). Fine for a personal, single-admin tool where you
only ever run your own code; do not paste in pipelines you would not otherwise execute directly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import duckdb
import pyarrow as pa

from .types import ColSpec, from_arrow

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
STEP_KINDS = ("rename", "cast", "filter", "derive", "split", "sql", "python")


class StepError(Exception):
    """A step's configuration or code is invalid, or it failed against a real batch."""


@dataclass
class Step:
    id: str
    kind: str
    config: dict[str, Any] = field(default_factory=dict)


def compile_steps(steps: list[Any]) -> list[Step]:
    """`steps` is a list of StepIn (or anything with .id/.kind/.config) -> plain Step objects."""
    out = []
    for s in steps:
        kind = s.kind
        if kind not in STEP_KINDS:
            raise StepError(f"Unknown step kind {kind!r}")
        out.append(Step(id=s.id, kind=kind, config=dict(s.config or {})))
    return out


def _qi(name: str) -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise StepError(f"{name!r} is not a plain column name (letters, digits, underscore, not starting with a digit)")
    return '"' + name + '"'


def _sql_for(step: Step) -> str:
    k, c = step.kind, step.config
    if k == "sql":
        text = (c.get("sql") or "").strip()
        if not text:
            raise StepError("SQL step needs a query")
        if "{{ input }}" not in text and "{{input}}" not in text:
            raise StepError("SQL step must read FROM {{ input }} (the previous step's output)")
        return text.replace("{{ input }}", "input").replace("{{input}}", "input")
    if k == "rename":
        mapping = c.get("mapping") or {}
        if not mapping:
            raise StepError("Rename step needs at least one column")
        ren = ", ".join(f"{_qi(a)} AS {_qi(b)}" for a, b in mapping.items())
        return f"SELECT * RENAME ({ren}) FROM input"
    if k == "cast":
        mapping = c.get("mapping") or {}
        if not mapping:
            raise StepError("Cast step needs at least one column")
        rep = ", ".join(f"CAST({_qi(col)} AS {sqltype}) AS {_qi(col)}" for col, sqltype in mapping.items())
        return f"SELECT * REPLACE ({rep}) FROM input"
    if k == "filter":
        expr = (c.get("expr") or "").strip()
        if not expr:
            raise StepError("Filter step needs a condition")
        return f"SELECT * FROM input WHERE {expr}"
    if k == "derive":
        name, expr = c.get("column"), (c.get("expr") or "").strip()
        if not name or not expr:
            raise StepError("Derive step needs a column name and an expression")
        return f"SELECT *, ({expr}) AS {_qi(name)} FROM input"
    if k == "split":
        parts = c.get("into") or []
        if not parts:
            raise StepError("Split step needs at least one output column")
        extra = ", ".join(f"({p['expr']}) AS {_qi(p['name'])}" for p in parts)
        base = f"* EXCLUDE ({_qi(c['column'])})" if c.get("drop_source") and c.get("column") else "*"
        return f"SELECT {base}, {extra} FROM input"
    raise StepError(f"Unknown step kind {k!r}")


def _run_sql(sql: str, table: pa.Table) -> pa.Table:
    con = duckdb.connect()
    try:
        con.register("input", table)
        try:
            res = con.sql(sql).arrow()
        except duckdb.Error as e:
            raise StepError(str(e)) from e
        # DuckDB's Python API returns a pyarrow.Table on some versions and a RecordBatchReader
        # (streaming, one-shot) on others; normalize to a Table either way.
        return res.read_all() if isinstance(res, pa.RecordBatchReader) else res
    finally:
        con.close()


def _run_python(code: str, table: pa.Table) -> pa.Table:
    import pandas as pd

    ns: dict[str, Any] = {}
    try:
        exec(compile(code, "<pipeline step>", "exec"), ns)  # noqa: S102 - the user's own step, run against their own data
    except SyntaxError as e:
        raise StepError(f"Python step does not parse: {e}") from e
    fn = ns.get("transform")
    if not callable(fn):
        raise StepError("Python step must define def transform(df): ...")
    df = table.to_pandas()
    try:
        out = fn(df)
    except Exception as e:  # noqa: BLE001 - any failure in the user's own code, surfaced as a step error
        raise StepError(f"Python step failed: {type(e).__name__}: {e}") from e
    if not isinstance(out, pd.DataFrame):
        raise StepError("transform(df) must return a DataFrame")
    try:
        return pa.Table.from_pandas(out, preserve_index=False)
    except (pa.ArrowInvalid, pa.ArrowTypeError) as e:
        raise StepError(f"transform(df) returned a DataFrame Arrow cannot represent: {e}") from e


def apply_steps(steps: list[Step], table: pa.Table) -> pa.Table:
    """Run every step in order over one batch. Raises StepError on the first one that fails."""
    for step in steps:
        table = _run_python(step.config.get("code") or "", table) if step.kind == "python" else _run_sql(_sql_for(step), table)
    return table


def infer_colspecs(table: pa.Table) -> list[ColSpec]:
    """The column list a step chain's output implies, read straight off the Arrow schema of a real sample."""
    return [ColSpec(name=f.name, logical=from_arrow(f.type), nullable=f.nullable) for f in table.schema]
