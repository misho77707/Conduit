"""The job request: what the wizard sends, validated once and stored on the job."""
from __future__ import annotations

import datetime as dt
import decimal
import uuid
import zoneinfo
from typing import Any, Literal

import re

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


class ParseOptions(BaseModel):
    """How a file is read: shared by local files (FileIn) and remote ones (RemoteIn)."""

    format: Literal["csv", "json", "parquet", "excel"] | None = None  # detected from the name when empty
    delimiter: str | None = Field(None, max_length=8)
    header: bool = True
    encoding: str = "utf-8"
    sheet: str | None = None
    skip_rows: int = Field(0, ge=0, le=100_000)
    null_values: list[str] = Field(default_factory=lambda: [""])

    def parse_options(self) -> dict[str, Any]:
        return self.model_dump(include=set(ParseOptions.model_fields))


class FileIn(ParseOptions):
    path: str


_GLOB = ("*", "?", "[")


class RemoteIn(ParseOptions):
    """A file on S3 / SFTP / behind a URL. Credentials stay in the connection; only the path is stored here."""

    connection_id: uuid.UUID | None = None  # an S3 / SFTP / HTTP connection; None = an ad-hoc public http(s) URL
    path: str = Field(min_length=1, max_length=4000)  # key / remote path / URL / pattern, relative to the base
    match: Literal["exact", "newest", "all"] = "exact"  # one file / the newest match / every match (one job)
    skip_loaded: bool = True  # newest / all: leave out files already loaded into the same target

    @model_validator(mode="after")
    def _check(self) -> "RemoteIn":
        p = self.path.strip()
        url = p.lower().startswith(("http://", "https://"))
        if self.connection_id is None:
            if not url:
                raise ValueError("A remote file without a connection must be a full http:// or https:// URL")
            if self.match != "exact":
                raise ValueError("A URL is one file: 'newest' and 'all' need an S3 or SFTP connection")
        if not url and any(s == ".." for s in p.replace("\\", "/").split("/")):
            raise ValueError("Remote paths may not contain '..': they are relative to the connection's base folder")
        pattern = not url and any(ch in p for ch in _GLOB)
        if pattern and self.match == "exact":
            raise ValueError("The path is a pattern: choose 'newest' or 'all' matching files, or give one exact file")
        self.path = p
        return self


_ALIAS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class CombinedInputIn(BaseModel):
    """One input of a combined source: a name the SQL uses as a table, and where its rows come from."""

    alias: str
    source: "SourceIn"  # a table / query / file / remote file (never combined); its `where` is pushed down
    columns: list[str] | None = None  # read only these columns (None = all), pushed down to the source

    @field_validator("alias")
    @classmethod
    def _alias(cls, v: str) -> str:
        if not _ALIAS.match(v or ""):
            raise ValueError(f"Alias {v!r} must be a plain identifier (letters, digits, underscore; not starting with a digit)")
        return v

    @model_validator(mode="after")
    def _not_nested(self) -> "CombinedInputIn":
        if self.source.combined is not None:
            raise ValueError(f"Input {self.alias!r} is itself combined: inputs must be tables, queries or files")
        return self


class CombinedWatermarkIn(BaseModel):
    """Read only the driver's rows above the last saved value of `column` (the first run reads everything)."""

    column: str = Field(min_length=1, max_length=200)
    # Re-read this much below the saved watermark, for late-arriving rows (pair it with upsert): seconds for a
    # date/time column, the column's own units for a number.
    overlap: float = Field(0, ge=0)


class CombinedIn(BaseModel):
    """Several inputs joined by one DuckDB SELECT over their aliases (pipelines only)."""

    inputs: list[CombinedInputIn] = Field(min_length=2, max_length=8)
    sql: str = Field(min_length=1, max_length=100_000)
    mode: Literal["staged", "stream"] = "staged"  # staged: copy all inputs, join once; stream: driver in batches
    driver: str | None = None  # an alias: streamed in stream mode, and the input a watermark applies to
    watermark: CombinedWatermarkIn | None = None

    @model_validator(mode="after")
    def _shape(self) -> "CombinedIn":
        aliases = [i.alias for i in self.inputs]
        dupes = sorted({a for a in aliases if [x.lower() for x in aliases].count(a.lower()) > 1})
        if dupes:
            raise ValueError(f"Each input needs its own alias ({', '.join(dupes)} is used twice)")
        if self.driver is not None and self.driver not in aliases:
            raise ValueError(f"The driver {self.driver!r} is not one of the inputs ({', '.join(aliases)})")
        if self.mode == "stream" and self.driver is None:
            raise ValueError("Stream mode needs a driver: the one big input that is streamed (the others are lookups)")
        if self.watermark is not None and self.driver is None:
            raise ValueError("A watermark needs a driver: the input whose new rows each run reads")
        return self

    def input(self, alias: str) -> CombinedInputIn:
        return next(i for i in self.inputs if i.alias == alias)


class SourceIn(BaseModel):
    connection_id: uuid.UUID | None = None
    table: str | None = None
    query: str | None = None
    where: str = ""  # SQL condition (MongoDB: a JSON filter)
    file: FileIn | None = None
    remote: RemoteIn | None = None
    combined: CombinedIn | None = None  # pipelines only: several inputs joined by SQL

    @model_validator(mode="after")
    def _one_source(self) -> "SourceIn":
        given = [x for x in (self.table, self.query, self.file, self.remote, self.combined) if x]
        if len(given) != 1:
            raise ValueError("Give exactly one of: table, query, file, remote, combined")
        if self.file is None and self.remote is None and self.combined is None and self.connection_id is None:
            raise ValueError("connection_id is required for a table or query source")
        return self

    @property
    def kind(self) -> str:
        return ("file" if self.file else "remote" if self.remote else "combined" if self.combined
                else "query" if self.query else "table")

    @property
    def is_file(self) -> bool:
        """A local or a remote file (read through the file adapter, never a database table)."""
        return self.file is not None or self.remote is not None


CombinedInputIn.model_rebuild()  # it refers to SourceIn, defined after it


class TargetIn(BaseModel):
    connection_id: uuid.UUID
    table: str = Field(min_length=1, max_length=300)
    mode: Literal["append", "replace", "upsert"] = "append"
    keys: list[str] = Field(default_factory=list)
    create: bool = True  # create the table when it does not exist
    order_by: list[str] = Field(default_factory=list)  # ClickHouse ORDER BY
    partition_by: str | None = Field(None, max_length=200)  # ClickHouse PARTITION BY expression
    # Hand-edited CREATE statements (';'-separated) run instead of the generated DDL when the table is created
    ddl: str | None = Field(None, max_length=100_000)

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
    # driver option per side for this run (engine/drivers.py); None: the connection's own setting
    source_driver: str | None = Field(None, max_length=20)
    target_driver: str | None = Field(None, max_length=20)
    # how rows move: through Conduit (None / "conduit"), or ClickHouse reads the source itself (engine/chpull.py)
    transfer_method: Literal["conduit", "clickhouse_pull"] | None = None

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

    @model_validator(mode="after")
    def _engine(self) -> "StepIn":
        engine = self.config.get("engine")
        if self.kind == "python" and engine is not None and engine not in ("pandas", "polars"):
            raise ValueError(f"A Python step's engine is pandas or polars, not {engine!r}")
        return self


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
    # A combined source's cached output schema (the join SQL's columns, from the pipeline's last full preview):
    # a combined source can't be probed without staging its inputs, so launching a run never reads them.
    source_columns: list[StepOutCol] | None = None
    # The step chain's cached output schema (from the pipeline's last preview); required when `steps` is
    # non-empty, since that is what `columns`-less identity mapping maps onto the target table.
    step_output_columns: list[StepOutCol] | None = None

    @model_validator(mode="after")
    def _steps_need_a_column_cache(self) -> "JobIn":
        if self.source.combined is not None and not self.source_columns:
            raise ValueError("This combined pipeline has no cached preview yet: refresh the preview before running it")
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
