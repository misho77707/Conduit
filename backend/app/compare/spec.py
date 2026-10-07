"""The compare request: two sides, which columns to pair, how rows are matched, and comparison options."""
from __future__ import annotations

import uuid

from pydantic import BaseModel, Field, model_validator

from ..engine.spec import SourceIn


class ColumnPair(BaseModel):
    source: str  # column name on the source side
    target: str  # column name on the target side (also the name used in results)
    include: bool = True


class CompareOptions(BaseModel):
    ignore_nulls: bool = False  # a NULL on either side is not a difference (key matching only)
    check_duplicates: bool = True  # report keys (or whole rows) that occur more than once on a side
    trim_text: bool = False  # compare text without leading / trailing whitespace
    case_insensitive: bool = False  # compare text ignoring upper / lower case
    float_decimals: int = Field(6, ge=0, le=15)  # FLOAT / DOUBLE values are rounded to this many decimals first
    sample_rows: int = Field(100, ge=1, le=10_000)  # rows shown per result set, and exported for "sample only"
    read_chunk: int = Field(50_000, ge=1_000, le=1_000_000)  # rows fetched per round trip from each side


class CompareIn(BaseModel):
    name: str | None = Field(None, max_length=200)
    source: SourceIn
    target: SourceIn
    # None pairs every column whose name exists on both sides (case-insensitive)
    columns: list[ColumnPair] | None = None
    # Target-side column names. None = the target's primary key (else the source's); [] = compare whole rows
    keys: list[str] | None = None
    options: CompareOptions = Field(default_factory=CompareOptions)
    job_id: uuid.UUID | None = None  # set when started from a job's "Verify" button

    @model_validator(mode="after")
    def _target_is_readable(self) -> "CompareIn":
        if self.keys is not None and len(set(self.keys)) != len(self.keys):
            raise ValueError("A key column is listed twice")
        return self


class SidesIn(BaseModel):
    """Just the two sides: what the setup screen sends to learn their columns and the suggested key."""

    source: SourceIn
    target: SourceIn
