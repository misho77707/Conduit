"""The job request: what the wizard sends, validated once and stored on the job."""
from __future__ import annotations

import datetime as dt
import decimal
import uuid
import zoneinfo
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .types import clean, is_valid


class ColumnMap(BaseModel):
    source: str
    name: str | None = None  # target column name (defaults to the source name)
    type: str | None = None  # target logical type (defaults to what the source suggests)
    nullable: bool | None = None
    include: bool = True
    format: str | None = Field(None, max_length=60)  # strptime pattern for text -> DATE / TIMESTAMP, e.g. %d.%m.%Y

    @field_validator("type")
    @classmethod
    def _type_ok(cls, v: str | None) -> str | None:
        if v is not None and not is_valid(v):
            raise ValueError(f"Unknown column type {v!r}")
        return clean(v) if v else v


class FileIn(BaseModel):
    path: str
    format: Literal["csv", "json", "parquet", "excel"] | None = None
    delimiter: str | None = Field(None, max_length=8)
    header: bool = True
    encoding: str = "utf-8"
    sheet: str | None = None
    skip_rows: int = Field(0, ge=0, le=100_000)
    null_values: list[str] = Field(default_factory=lambda: [""])


class SourceIn(BaseModel):
    connection_id: uuid.UUID | None = None
    table: str | None = None
    query: str | None = None
    where: str = ""  # SQL condition (MongoDB: a JSON filter)
    file: FileIn | None = None

    @model_validator(mode="after")
    def _one_source(self) -> "SourceIn":
        given = [x for x in (self.table, self.query, self.file) if x]
        if len(given) != 1:
            raise ValueError("Give exactly one of: table, query, file")
        if self.file is None and self.connection_id is None:
            raise ValueError("connection_id is required for a table or query source")
        return self

    @property
    def kind(self) -> str:
        return "file" if self.file else "query" if self.query else "table"


class TargetIn(BaseModel):
    connection_id: uuid.UUID
    table: str = Field(min_length=1, max_length=300)
    mode: Literal["append", "replace", "upsert"] = "append"
    keys: list[str] = Field(default_factory=list)
    create: bool = True  # create the table when it does not exist
    order_by: list[str] = Field(default_factory=list)  # ClickHouse ORDER BY
    partition_by: str | None = Field(None, max_length=200)  # ClickHouse PARTITION BY expression

    @model_validator(mode="after")
    def _keys(self) -> "TargetIn":
        if self.mode == "upsert" and not self.keys:
            raise ValueError("Upsert needs at least one key column")
        return self


class OptionsIn(BaseModel):
    strategy: Literal["full", "id", "time", "watermark"] = "full"
    split_column: str | None = None  # column to cut the source into parallel ranges (any strategy)
    lo: str | int | float | None = None  # id/time: from (inclusive)
    hi: str | int | float | None = None  # id: to (inclusive); time: to (exclusive)
    timezone: str = "UTC"  # how naive time bounds are read
    workers: int = Field(4, ge=1, le=64)
    splits: int | None = Field(None, ge=1, le=2000)
    split_rows: int | None = Field(None, ge=1000)  # aim for this many rows per split (overrides `splits`)
    read_chunk: int = Field(50_000, ge=100, le=1_000_000)  # rows fetched per round trip
    write_batch: int = Field(50_000, ge=100, le=1_000_000)  # rows per insert / commit
    max_rejects: int = Field(1000, ge=-1)  # stop after this many rejected rows (-1 = never stop)
    retries: int = Field(3, ge=0, le=10)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            zoneinfo.ZoneInfo(v)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"Unknown time zone {v!r}") from e
        return v

    @model_validator(mode="after")
    def _needs_column(self) -> "OptionsIn":
        if self.strategy in ("id", "time", "watermark") and not self.split_column:
            raise ValueError(f"The {self.strategy!r} strategy needs a split_column")
        return self


class StepIn(BaseModel):
    """One pipeline transform step, run per batch (see engine/steps.py for the supported kinds)."""

    id: str = Field(min_length=1, max_length=40)
    kind: Literal["rename", "cast", "filter", "derive", "split", "sql", "python"]
    config: dict[str, Any] = Field(default_factory=dict)


class StepOutCol(BaseModel):
    """One column of a step chain's cached output schema (Pipeline.output_columns), from the last preview."""

    name: str
    type: str
    nullable: bool = True
    source_name: str | None = None


class JobIn(BaseModel):
    name: str | None = Field(None, max_length=200)
    source: SourceIn
    target: TargetIn
    columns: list[ColumnMap] | None = None
    options: OptionsIn = Field(default_factory=OptionsIn)
    # Pipeline transform chain (empty for an ordinary transfer / file load job).
    steps: list[StepIn] = Field(default_factory=list)
    # The step chain's cached output schema (from the pipeline's last preview); required when `steps` is
    # non-empty, since that is what `columns`-less identity mapping maps onto the target table.
    step_output_columns: list[StepOutCol] | None = None

    @model_validator(mode="after")
    def _steps_need_a_column_cache(self) -> "JobIn":
        if self.steps and not self.step_output_columns:
            raise ValueError("This pipeline has no cached preview yet: refresh the preview before running it")
        if self.steps and self.options.split_column:
            raise ValueError("Pipelines read the whole source in one piece; a split column is not supported yet")
        return self


# ---- bounds ---------------------------------------------------------------------

def parse_bound(value: Any, logical: str, tz: str, *, upper: bool, strategy: str, after) -> Any:
    """User bound (string / number) -> typed value for the split column.

    ID ranges are inclusive at both ends (so an upper bound becomes 'just above it'); time ranges are
    [from, to). Naive times are read in `tz` and stored as UTC.
    """
    if value is None or value == "":
        return None
    t = clean(logical)
    if t in ("SMALLINT", "INTEGER", "BIGINT"):
        v: Any = int(value)
    elif t in ("FLOAT", "DOUBLE"):
        v = float(value)
    elif t.startswith("DECIMAL"):
        v = decimal.Decimal(str(value))
    elif t == "DATE":
        v = dt.date.fromisoformat(str(value)[:10])
    elif t in ("TIMESTAMP", "TIMESTAMPTZ"):
        v = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if v.tzinfo is None:
            v = v.replace(tzinfo=zoneinfo.ZoneInfo(tz))
        v = v.astimezone(dt.timezone.utc)
        if t == "TIMESTAMP":
            v = v.replace(tzinfo=None)
    else:
        raise ValueError(f"Column type {logical} cannot be used for ID or time ranges")
    if upper and strategy == "id":
        v = after(v)
    return v
