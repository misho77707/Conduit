"""Plain data classes shared by adapters and the runner."""
from __future__ import annotations

import datetime as dt
import decimal
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa

from .types import ColSpec


@dataclass
class ReadRequest:
    """What to read from a source, before it is cut into splits."""

    table: str | None = None
    query: str | None = None  # custom SQL source (wrapped as a subquery)
    columns: list[ColSpec] = field(default_factory=list)  # columns to read, source names
    where: str = ""  # extra user filter, raw SQL
    mode: str = "full"  # full | id | time
    split_col: str | None = None
    lo: Any = None  # inclusive lower bound
    hi: Any = None  # exclusive upper bound
    exclusive_lo: bool = False  # lower bound is a watermark: col > lo
    split_col_logical: str | None = None


@dataclass
class SplitSpec:
    """One unit of parallel work: a slice of the source read and written independently."""

    idx: int
    label: str
    kind: str = "full"  # full | range | ctid | partition
    column: str | None = None
    lo: Any = None
    hi: Any = None
    exclusive_lo: bool = False
    est_rows: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)  # partition ids, ctid block range ...
    resumable: bool = False  # ordered by a key, so a retry can continue after the last committed batch

    def to_json(self) -> dict[str, Any]:
        return {"idx": self.idx, "label": self.label, "kind": self.kind, "column": self.column,
                "lo": enc(self.lo), "hi": enc(self.hi), "exclusive_lo": self.exclusive_lo,
                "est_rows": self.est_rows, "extra": self.extra, "resumable": self.resumable}

    @staticmethod
    def from_json(d: dict[str, Any]) -> "SplitSpec":
        return SplitSpec(idx=d["idx"], label=d["label"], kind=d.get("kind", "full"), column=d.get("column"),
                         lo=dec(d.get("lo")), hi=dec(d.get("hi")), exclusive_lo=d.get("exclusive_lo", False),
                         est_rows=d.get("est_rows"), extra=d.get("extra") or {}, resumable=d.get("resumable", False))


@dataclass
class Batch:
    table: pa.Table
    resume: Any = None  # JSON-able key of the last row (in split order); None if the split is not resumable
    rejects: list[tuple[str, dict[str, Any]]] = field(default_factory=list)  # (reason, row) dropped while reading


# ---- JSON-safe encoding of bound / resume values -------------------------------

def enc(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        return {"t": "bool", "v": v}
    if isinstance(v, int):
        return {"t": "int", "v": v}
    if isinstance(v, float):
        return {"t": "float", "v": v}
    if isinstance(v, decimal.Decimal):
        return {"t": "dec", "v": str(v)}
    if isinstance(v, dt.datetime):
        return {"t": "dt", "v": v.isoformat()}
    if isinstance(v, dt.date):
        return {"t": "date", "v": v.isoformat()}
    if type(v).__name__ == "ObjectId":
        return {"t": "oid", "v": str(v)}
    return {"t": "str", "v": str(v)}


def dec(d: Any) -> Any:
    if d is None:
        return None
    t, v = d["t"], d["v"]
    return {"bool": lambda: v, "int": lambda: int(v), "float": lambda: float(v), "dec": lambda: decimal.Decimal(v),
            "dt": lambda: dt.datetime.fromisoformat(v), "date": lambda: dt.date.fromisoformat(v),
            "str": lambda: v, "oid": lambda: _oid(v)}[t]()


def _oid(v: str) -> Any:
    from bson import ObjectId
    return ObjectId(v)


@dataclass
class TargetSpec:
    table: str
    create: bool = True  # create the table when missing
    order_by: list[str] = field(default_factory=list)
    partition_by: str | None = None
