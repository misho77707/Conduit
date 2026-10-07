"""Column mapping between what a source emits and what the target table wants.

Renames, casts (with per-row failure detection), NOT NULL checks and VARCHAR(n) length checks. Rows that
cannot be converted are dropped from the batch and returned as rejects; nothing is silently changed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from .arrowconv import _jsonable, coerce
from .types import ColSpec, clean, to_arrow

_VARN = re.compile(r"^VARCHAR\((\d+)\)$")
_ARROW_ERRORS = (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError, OverflowError, ValueError, TypeError)


@dataclass
class OutCol:
    source: str  # column name in the source batch
    target: ColSpec  # name / logical type / nullability in the target
    fmt: str | None = None  # strptime format when a text column becomes DATE / TIMESTAMP
    # Value that replaces NULLs in a NOT NULL target column (its evaluated DEFAULT); NO_FILL keeps them rejects.
    fill: Any = None


NO_FILL = None


Reject = tuple[str, dict[str, Any]]


def _is_text(t: pa.DataType) -> bool:
    return pa.types.is_string(t) or pa.types.is_large_string(t)


def _clean_text(arr: pa.Array) -> pa.Array:
    """Trim whitespace; blank strings become NULL (used before casting text to a non-text type)."""
    trimmed = pc.utf8_trim_whitespace(arr)
    return pc.if_else(pc.equal(trimmed, ""), pa.scalar(None, type=trimmed.type), trimmed)


def cast_array(arr: pa.Array, t: pa.DataType, fmt: str | None = None) -> tuple[pa.Array, list[int]]:
    """Cast one column. Returns the cast array and the indices of values that could not be converted."""
    if arr.type == t:
        return arr, []
    if _is_text(arr.type) and not pa.types.is_string(t):
        arr = _clean_text(arr.cast(pa.string()))
        if fmt and (pa.types.is_timestamp(t) or pa.types.is_date(t)):
            res = pc.strptime(arr, format=fmt, unit="us", error_is_null=True)
            bad = pc.and_(arr.is_valid(), res.is_null())
            out = res.cast(t, safe=False)
            return out, [i for i, b in enumerate(bad.to_pylist()) if b]
    try:
        return arr.cast(t), []
    except _ARROW_ERRORS:
        pass
    values = arr.to_pylist()
    out: list[Any] = []
    bad: list[int] = []
    for i, v in enumerate(values):
        try:
            c = coerce(v, t)
            pa.array([c], type=t)  # validates range / representability
            out.append(c)
        except (*_ARROW_ERRORS, ArithmeticError, Exception):  # noqa: B014 - any conversion failure is a bad value
            out.append(None)
            if v is not None:
                bad.append(i)
    return pa.array(out, type=t), bad


class Transformer:
    def __init__(self, source_cols: list[ColSpec], out: list[OutCol]):
        self.source_cols = source_cols
        self.out = out

    @property
    def target_cols(self) -> list[ColSpec]:
        return [o.target for o in self.out]

    def apply(self, table: pa.Table) -> tuple[pa.Table, list[Reject]]:
        n = table.num_rows
        bad: dict[int, str] = {}
        arrays: list[pa.Array] = []
        for o in self.out:
            col = table.column(o.source).combine_chunks() if isinstance(table.column(o.source), pa.ChunkedArray) else table.column(o.source)
            t = to_arrow(o.target.logical)
            arr, bad_idx = cast_array(col, t, o.fmt)
            if bad_idx:
                srcs = col.to_pylist()
                for i in bad_idx:
                    bad.setdefault(i, f"column {o.target.name}: cannot convert {srcs[i]!r} to {clean(o.target.logical)}")
            m = _VARN.match(clean(o.target.logical))
            if m and pa.types.is_string(arr.type):
                over = pc.greater(pc.utf8_length(arr), int(m.group(1)))
                for i, flag in enumerate(over.to_pylist()):
                    if flag:
                        bad.setdefault(i, f"column {o.target.name}: value longer than {m.group(1)} characters")
            if not o.target.nullable and arr.null_count and o.fill is not NO_FILL:
                arr = pc.fill_null(arr, pa.scalar(o.fill, type=arr.type))
            if not o.target.nullable and arr.null_count:
                for i, flag in enumerate(arr.is_null().to_pylist()):
                    if flag:
                        bad.setdefault(i, f"column {o.target.name}: empty value in a NOT NULL column")
            arrays.append(arr)
        out = pa.Table.from_arrays(arrays, names=[o.target.name for o in self.out])
        if not bad:
            return out, []
        keep = pa.array([i not in bad for i in range(n)])
        src_rows = table.to_pylist()
        rejects = [(bad[i], {k: (None if v is None else _jsonable(v)) for k, v in src_rows[i].items()}) for i in sorted(bad)]
        return out.filter(keep), rejects
