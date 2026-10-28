"""Row <-> Arrow conversion and helpers shared by every adapter."""
from __future__ import annotations

import base64
import datetime as dt
import decimal
import json
import uuid
from typing import Any

import pyarrow as pa

from .types import ColSpec, to_arrow

_UTC = dt.timezone.utc


class ConversionError(ValueError):
    pass


def _jsonable(v: Any) -> Any:
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, (decimal.Decimal, uuid.UUID)):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(v)).decode()
    return str(v)


def coerce(v: Any, t: pa.DataType) -> Any:
    """Best-effort conversion of one Python value to what Arrow expects for type `t`."""
    if v is None:
        return None
    if pa.types.is_string(t):
        if isinstance(v, str):
            return v
        if isinstance(v, (dict, list, tuple)):
            return json.dumps(v, default=_jsonable, ensure_ascii=False)
        if isinstance(v, (bytes, bytearray, memoryview)):
            return bytes(v).hex()
        if isinstance(v, (dt.datetime, dt.date, dt.time)):
            return v.isoformat()
        return str(v)
    if pa.types.is_timestamp(t):
        if isinstance(v, str):
            v = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
        elif isinstance(v, dt.date) and not isinstance(v, dt.datetime):
            v = dt.datetime(v.year, v.month, v.day)
        if isinstance(v, dt.datetime):
            if t.tz and v.tzinfo is None:
                return v.replace(tzinfo=_UTC)
            if not t.tz and v.tzinfo is not None:
                return v.astimezone(_UTC).replace(tzinfo=None)
        return v
    if pa.types.is_date(t):
        if isinstance(v, dt.datetime):
            return v.date()
        if isinstance(v, str):
            return dt.date.fromisoformat(v[:10])
        return v
    if pa.types.is_integer(t):
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, (float, decimal.Decimal)) and v == int(v):
            return int(v)
        if isinstance(v, str):
            return int(v)
        return v
    if pa.types.is_floating(t):
        return float(v)
    if pa.types.is_decimal(t):
        if isinstance(v, decimal.Decimal):
            return v
        return decimal.Decimal(str(v))
    if pa.types.is_boolean(t):
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "t", "yes", "y")
        return bool(v)
    if pa.types.is_binary(t):
        return bytes(v) if not isinstance(v, str) else v.encode()
    return v


def to_array(values: list[Any], t: pa.DataType, col: str = "?") -> pa.Array:
    try:
        return pa.array(values, type=t)
    except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError, OverflowError, ValueError):
        pass
    try:
        return pa.array([coerce(v, t) for v in values], type=t)
    except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError, OverflowError, ValueError, decimal.InvalidOperation) as e:
        raise ConversionError(f"column {col}: cannot convert values to {t} ({e})") from e


def rows_to_table(rows: list[tuple], cols: list[ColSpec]) -> pa.Table:
    arrays = [to_array([r[j] for r in rows], to_arrow(c.logical), c.name) for j, c in enumerate(cols)]
    return pa.Table.from_arrays(arrays, names=[c.name for c in cols])


def rows_to_table_lenient(rows: list[tuple], cols: list[ColSpec]) -> tuple[pa.Table, list[tuple[str, dict[str, Any]]]]:
    """Like rows_to_table, but rows that cannot be converted are returned as rejects instead of failing the batch."""
    try:
        return rows_to_table(rows, cols), []
    except ConversionError:
        pass
    good: list[tuple] = []
    rejects: list[tuple[str, dict[str, Any]]] = []
    for row in rows:
        try:
            rows_to_table([row], cols)
            good.append(row)
        except ConversionError as e:
            rejects.append((str(e), {c.name: _jsonable(v) if v is not None else None for c, v in zip(cols, row)}))
    return rows_to_table(good, cols), rejects


def table_rows(table: pa.Table) -> list[tuple]:
    """Arrow table -> list of Python row tuples (column order preserved)."""
    cols = [c.to_pylist() for c in table.columns]
    return list(zip(*cols)) if cols else []


def conform(table: pa.Table, cols: list[ColSpec]) -> pa.Table:
    """Cast an Arrow table (e.g. from DuckDB/ClickHouse) to the exact logical schema; renames by position."""
    arrays = []
    for i, c in enumerate(cols):
        t = to_arrow(c.logical)
        arr = table.column(i)
        if arr.type != t:
            try:
                if pa.types.is_date(t) and pa.types.is_integer(arr.type):
                    arr = arr.cast(pa.int32()).cast(pa.date32())  # ClickHouse sends Date as day counts
                elif pa.types.is_timestamp(t) and pa.types.is_integer(arr.type):
                    arr = arr.cast(pa.int64()).cast(pa.timestamp("s")).cast(t)  # ... and DateTime as epoch seconds
                else:
                    arr = arr.cast(t, safe=False)
            except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
                arr = pa.chunked_array([to_array([coerce(v, t) for v in arr.to_pylist()], t, c.name)])
        arrays.append(arr)
    return pa.Table.from_arrays(arrays, names=[c.name for c in cols])


# ---- fast drivers: one place that makes their Arrow look exactly like the classic path's ----------------------

# A jsonb value whose canonical text may differ from what the classic path makes of it (json.loads, then coerce):
# a top-level string / true / false / null, or a number with a fraction or an exponent (floats print differently).
_JSON_MAY_DIFFER = r'^\s*"|^\s*(?:true|false|null)\s*$|[0-9]\.[0-9]|[0-9][eE][-+]?[0-9]'


def classic_json(text: str | None) -> Any:
    """JSON text -> what the classic path makes of the parsed value (psycopg parses json / jsonb with json.loads)."""
    return None if text is None else coerce(json.loads(text), pa.string())


def json_like_classic(arr: pa.Array, canonical: bool) -> pa.Array:
    """`canonical`: the text is Postgres jsonb output (sorted keys, ", " / ": "), so only values that may print
    differently go through Python; otherwise (json: the text as it was stored) every value does."""
    import pyarrow.compute as pc

    arr = arr.combine_chunks() if isinstance(arr, pa.ChunkedArray) else arr
    if not canonical:
        return pa.array([classic_json(v) for v in arr.to_pylist()], pa.string())
    mask = pc.fill_null(pc.match_substring_regex(arr, _JSON_MAY_DIFFER), False)
    if not pc.any(mask).as_py():
        return arr
    idx = pc.indices_nonzero(mask)
    fixed = pa.array([classic_json(v) for v in arr.take(idx).to_pylist()], pa.string())
    return pc.replace_with_mask(arr, mask, fixed)


def plain(arr: pa.ChunkedArray | pa.Array) -> pa.Array:
    """Driver-specific Arrow types -> the plain ones: extension types to their storage (Postgres numeric arrives
    as text in an opaque extension), large_string / large_binary to string / binary, one chunk."""
    arr = arr.combine_chunks() if isinstance(arr, pa.ChunkedArray) else arr
    if isinstance(arr.type, pa.BaseExtensionType):
        arr = arr.storage
    if pa.types.is_large_string(arr.type):
        arr = arr.cast(pa.string())
    elif pa.types.is_large_binary(arr.type):
        arr = arr.cast(pa.binary())
    return arr


def _is(check: str, t: pa.DataType) -> bool:
    """pa.types checks that only newer pyarrow versions have (the view layouts)."""
    return bool(getattr(pa.types, check, lambda _t: False)(t))


def plain_type(t: pa.DataType) -> pa.DataType:
    """The plain Arrow type the rest of the engine expects in place of a library's own layout: 64-bit-offset and
    view strings / binaries / lists, dictionaries (a Polars categorical) and extension types, at any depth."""
    if isinstance(t, pa.BaseExtensionType):
        return plain_type(t.storage_type)
    if pa.types.is_dictionary(t):
        return plain_type(t.value_type)
    if pa.types.is_large_string(t) or _is("is_string_view", t):
        return pa.string()
    if pa.types.is_large_binary(t) or _is("is_binary_view", t):
        return pa.binary()
    if pa.types.is_list(t) or pa.types.is_large_list(t) or _is("is_list_view", t) or _is("is_large_list_view", t):
        return pa.list_(plain_type(t.value_type))
    if pa.types.is_struct(t):
        return pa.struct([pa.field(f.name, plain_type(f.type), f.nullable) for f in t])
    return t


def plain_table(table: pa.Table) -> pa.Table:
    """`table` with plain_type() columns and no schema / field metadata (e.g. what Polars' to_arrow() returns)."""
    schema = pa.schema([pa.field(f.name, plain_type(f.type), f.nullable) for f in table.schema])
    return table.cast(schema) if schema != table.schema.remove_metadata() else table.replace_schema_metadata(None)


def from_driver(table: pa.Table, cols: list[ColSpec], json_cols: dict[str, bool] | None = None
                ) -> tuple[pa.Table, list[tuple[str, dict[str, Any]]]]:
    """A fast driver's batch -> exactly what the classic path would make of the same rows.

    Columns are matched by position. `json_cols`: name -> canonical (jsonb) for JSON columns. Casts are safe
    (nothing truncated); when one fails, the batch goes the classic row-by-row way, so bad values become the
    same rejects with the same reasons."""
    arrays = []
    for i, c in enumerate(cols):
        arr = plain(table.column(i))
        if json_cols and c.name in json_cols:
            arr = json_like_classic(arr, json_cols[c.name])
        arrays.append(arr)
    try:
        out = []
        for c, arr in zip(cols, arrays):
            t = to_arrow(c.logical)
            out.append(arr if arr.type == t else arr.cast(t, safe=True))
        return pa.Table.from_arrays(out, names=[c.name for c in cols]), []
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError, decimal.InvalidOperation):
        rows = list(zip(*[a.to_pylist() for a in arrays])) if arrays else []
        return rows_to_table_lenient(rows, cols)


def last_value(table: pa.Table, col: str) -> Any:
    if table.num_rows == 0:
        return None
    return table.column(col)[table.num_rows - 1].as_py()


def dedupe_last(table: pa.Table, keys: list[str]) -> pa.Table:
    """Keep the last row per key inside one batch (an upsert cannot touch the same row twice)."""
    if not keys or table.num_rows < 2:
        return table
    kcols = [table.column(k).to_pylist() for k in keys]
    seen: dict[tuple, int] = {}
    for i, key in enumerate(zip(*kcols)):
        seen[key] = i
    if len(seen) == table.num_rows:
        return table
    return table.take(pa.array(sorted(seen.values())))
