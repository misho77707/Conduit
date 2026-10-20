import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    username: str
    role: str


class ConnectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    type: str
    env: str | None = Field(default=None, max_length=30)
    config: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, Any] = Field(default_factory=dict)


class ConnectionPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    env: str | None = None
    config: dict[str, Any] | None = None  # merged over the stored config
    secrets: dict[str, Any] | None = None  # merged; omitted keys keep their stored value


class ConnectionTestIn(BaseModel):
    """Test an unsaved connection (the 'Test connection' button in the add flow)."""
    type: str
    config: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, Any] = Field(default_factory=dict)


class ConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    type: str
    env: str | None
    host: str  # display_host
    category: str = "database"  # database | storage (S3 / SFTP / HTTP: a source of files, never a table)
    database: str | None
    config: dict[str, Any]
    status: str
    latency_ms: float | None
    server_version: str | None
    note: str | None
    last_checked_at: datetime | None
    created_at: datetime


class TestResultOut(BaseModel):
    ok: bool
    status: str
    latency_ms: float | None = None
    version: str | None = None
    detail: str | None = None
    error: str | None = None
    degraded: bool = False
    learned: dict[str, Any] = Field(default_factory=dict)  # config a test discovered (SFTP host key): save it with the connection


class ConnectionTestOut(TestResultOut):
    connection: ConnectionOut | None = None


class TableOut(BaseModel):
    name: str
    est_rows: int | None = None
    size_bytes: int | None = None
    exact: bool = False
    info: str = ""


class ColumnOut(BaseModel):
    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False
    indexed: bool = False


class SplitCandidateOut(BaseModel):
    column: str
    mode: str
    verdict: str
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TableSchemaOut(BaseModel):
    name: str
    columns: list[ColumnOut]
    row_estimate: int | None = None
    exact: bool = False
    size_bytes: int | None = None
    candidates: list[SplitCandidateOut] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class FileInspectIn(BaseModel):
    path: str = Field(min_length=1)
    sample_rows: int = Field(default=20, ge=1, le=1000)
    # CSV overrides the wizard re-inspects with (None = auto-detect)
    header: bool | None = None
    skip_rows: int | None = Field(default=None, ge=0, le=100_000)
    delimiter: str | None = Field(default=None, max_length=8)


class FileEntryOut(BaseModel):
    name: str
    path: str
    is_dir: bool
    size_bytes: int | None = None
    modified_at: datetime | None = None
    format: str | None = None  # None for directories and unsupported extensions (a .zip: the format inside it)
    compression: str | None = None  # gzip | zip


class FileInspectOut(BaseModel):
    path: str
    format: str
    compression: str | None = None  # gzip | zip
    columns: list[ColumnOut]
    row_estimate: int | None = None
    exact: bool = False
    size_bytes: int | None = None
    preview: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ---- pipelines ------------------------------------------------------------------

class PipelineIn(BaseModel):
    """Everything a pipeline needs except the cached preview schema (that comes from /preview)."""
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    source: dict[str, Any]  # engine.spec.SourceIn shape
    target: dict[str, Any]  # engine.spec.TargetIn shape
    options: dict[str, Any] = Field(default_factory=dict)  # engine.spec.OptionsIn shape
    steps: list[dict[str, Any]] = Field(default_factory=list)  # engine.spec.StepIn shape, read -> load order
    schedule: str | None = None
    enabled: bool = True


class PipelinePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    source: dict[str, Any] | None = None
    target: dict[str, Any] | None = None
    options: dict[str, Any] | None = None
    steps: list[dict[str, Any]] | None = None
    schedule: str | None = None
    enabled: bool | None = None


class PipelineLastRunOut(BaseModel):
    job_id: uuid.UUID
    status: str
    rows_written: int
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None


class PipelineOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    source: dict[str, Any]
    target: dict[str, Any]
    options: dict[str, Any]
    steps: list[dict[str, Any]]
    output_columns: list[dict[str, Any]] | None
    source_columns: list[dict[str, Any]] | None = None  # a combined source's join schema, from the last full preview
    schedule: str | None
    enabled: bool
    next_run_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    last_run: PipelineLastRunOut | None = None
    # the scheduler's last look at a remote newest / all source that had nothing new (no job was created then)
    last_checked_at: datetime | None = None
    last_check_note: str | None = None


class PipelinePreviewIn(BaseModel):
    sample_rows: int = Field(default=50, ge=1, le=2000)
    upto_step: str | None = None  # apply steps up to and including this step id; None = the whole chain


class PipelinePreviewOut(BaseModel):
    columns: list[dict[str, Any]]  # ColSpec.to_dict() shape
    rows: list[dict[str, Any]]
    sampled_rows: int
    full_chain: bool  # whether this ran every step (and so refreshed the pipeline's cached output schema)
    error: str | None = None  # why a step failed; columns/rows are then the output of the step before it
    error_step: str | None = None  # id of the step that failed
    # combined sources: per-input rows staged (capped?) and their columns, the join's row count, notes
    combined: dict[str, Any] | None = None
    source_columns: list[dict[str, Any]] | None = None  # the join SQL's exact output schema
