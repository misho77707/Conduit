import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    """GET /auth/me: who you are, and whether you must set a new password before anything else works."""

    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    username: str
    role: str  # superadmin | admin | viewer
    must_change_password: bool = False


Role = Literal["superadmin", "admin", "viewer"]


class PasswordChangeIn(BaseModel):
    current_password: str = Field(max_length=500)
    new_password: str = Field(max_length=500)


class UserCreateIn(BaseModel):
    username: str = Field(max_length=80)
    role: Role
    temporary_password: str = Field(max_length=500)  # the user must replace it at their first sign-in


class UserPatchIn(BaseModel):
    role: Role | None = None
    is_active: bool | None = None


class ResetPasswordIn(BaseModel):
    temporary_password: str = Field(max_length=500)


class UserAdminOut(BaseModel):
    """A row of the Users screen (superadmins only)."""

    id: uuid.UUID
    username: str
    role: Role
    is_active: bool
    must_change_password: bool
    created_at: datetime
    created_by: str | None  # username; None for the first-run account or when its creator was deleted
    last_login_at: datetime | None


class ConnectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    type: str
    env: str | None = Field(default=None, max_length=30)
    driver: str | None = None  # "auto" (the default where there is a choice) or a driver option id
    config: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, Any] = Field(default_factory=dict)


class ConnectionPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    env: str | None = None
    driver: str | None = None  # omitted: unchanged
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
    needs_credentials: bool = False  # imported without its secrets: unusable until edited with them
    driver: str | None = None  # "auto", a driver option id, or None (the type has one driver)


class TestResultOut(BaseModel):
    ok: bool
    status: str
    latency_ms: float | None = None
    version: str | None = None
    detail: str | None = None
    error: str | None = None
    degraded: bool = False
    learned: dict[str, Any] = Field(default_factory=dict)  # config a test discovered (SFTP host key): save it with the connection
    note: str | None = None  # e.g. the connection's fast driver could not be opened


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


class PipelineBriefOut(BaseModel):
    """GET /pipelines (every role): what the list shows. The source, steps and target are on GET /pipelines/{id}."""

    id: uuid.UUID
    name: str
    description: str
    source_kind: Literal["table", "query", "file", "remote", "combined"]
    source_label: str  # e.g. "orders (prod-pg)", "events (prod-ch) + users (prod-pg)", "orders_*.csv (minio)"
    target_label: str  # e.g. "public.orders (ch)"
    step_count: int
    runnable: bool  # a full preview cached the schema /run and the scheduler need
    schedule: str | None
    enabled: bool
    next_run_at: datetime | None = None
    last_run: PipelineLastRunOut | None = None
    last_checked_at: datetime | None = None
    last_check_note: str | None = None
    created_by: str | None  # None: created before owners were recorded, or by a deleted user
    created_at: datetime
    updated_at: datetime


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


# ---- alerts (Control & Setup) ------------------------------------------------------------------------------

AlertKind = Literal["telegram", "slack", "email", "webhook"]
AlertEvent = Literal["job_failed", "job_stalled", "schedule_failed", "input_stale", "connection_down", "check_down",
                     "host_high", "compare_diff"]


class AlertChannelIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: AlertKind
    config: dict[str, Any] = Field(default_factory=dict)  # telegram: chat_id; email: host, port, tls, user, from, to
    secrets: dict[str, Any] = Field(default_factory=dict)  # bot_token / webhook_url / password / url, headers, signing_secret
    enabled: bool = True


class AlertChannelPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    config: dict[str, Any] | None = None
    secrets: dict[str, Any] | None = None  # a blank or missing secret keeps the stored one
    clear_secrets: list[str] = Field(default_factory=list)  # optional secrets to remove (webhook headers / signing secret)
    enabled: bool | None = None


class AlertChannelTestIn(AlertChannelIn):
    id: uuid.UUID | None = None  # testing an edit: blank secrets are taken from this saved channel


class AlertChannelOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: AlertKind
    config: dict[str, Any]
    secrets_set: list[str]  # which secrets are stored (never their values)
    enabled: bool
    last_sent_at: datetime | None
    last_error: str | None
    rules: list[str]  # names of the rules that send here
    created_at: datetime
    updated_at: datetime


class AlertRuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    event: AlertEvent
    scope: dict[str, Any] = Field(default_factory=lambda: {"all": True})  # {"all": true} or {"pipelines": [ids], ...}
    options: dict[str, Any] = Field(default_factory=dict)  # thresholds; defaults filled in (app/alerts/catalog.py)
    channel_ids: list[uuid.UUID] = Field(default_factory=list)
    enabled: bool = True


class AlertRulePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    event: AlertEvent | None = None
    scope: dict[str, Any] | None = None
    options: dict[str, Any] | None = None
    channel_ids: list[uuid.UUID] | None = None
    enabled: bool | None = None


class AlertRuleOut(BaseModel):
    id: uuid.UUID
    name: str
    event: AlertEvent
    event_label: str
    scope: dict[str, Any]
    options: dict[str, Any]
    channel_ids: list[uuid.UUID]
    enabled: bool
    firing: int  # subjects firing right now
    created_at: datetime
    updated_at: datetime


class AlertTestIn(BaseModel):
    channel_ids: list[uuid.UUID] = Field(min_length=1)
    rule_name: str | None = None


class AlertSendResult(BaseModel):
    channel_id: uuid.UUID | None
    name: str
    ok: bool
    error: str | None


class AlertMuteIn(BaseModel):
    until: datetime | None  # null = unmute now (one summary of what is still firing goes out)


class AlertMuteOut(BaseModel):
    until: datetime | None


class ActiveAlertOut(BaseModel):
    rule_id: uuid.UUID | None  # None for a viewer
    rule_name: str
    event: AlertEvent
    event_label: str
    subject_kind: str
    subject_id: str | None  # None for a viewer
    subject_label: str
    since: datetime | None
    fired_at: datetime | None
    reason: str
    link: str  # app page, e.g. "#/jobs/<id>"


class ActiveAlertsOut(BaseModel):
    muted_until: datetime | None
    alerts: list[ActiveAlertOut]


# ---- settings (Control & Setup) -------------------------------------------------------------------------------

class SettingOut(BaseModel):
    key: str
    env: str  # the .env variable, e.g. CONDUIT_KEEP_FINISHED_JOBS
    group: str
    group_label: str
    label: str
    help: str
    cls: Literal["runtime", "restart"]  # runtime: editable here; restart: set in .env, then restart
    type: Literal["int", "float", "text"]
    unit: str
    min: float | None
    max: float | None
    value: Any  # the effective value
    default: Any
    env_value: Any = None  # what .env sets, when it sets something other than the default
    source: Literal["ui", "env", "default"]
    note: str | None = None
    updated_by: str | None = None  # a UI override: who and when
    updated_at: datetime | None = None


class SettingIn(BaseModel):
    value: Any  # checked against the setting's type and bounds (422 with a one-line reason)


# ---- export / import (Control & Setup) ------------------------------------------------------------------------

class ExportIn(BaseModel):
    include_secrets: bool = False  # superadmins only; needs a passphrase (the instance key never leaves)
    passphrase: str | None = None


class ImportIn(BaseModel):
    document: dict[str, Any]  # the exported file's JSON
    passphrase: str | None = None  # opens the file's secrets; without it they are left out


class ImportApplyIn(ImportIn):
    picked: list[str]  # item keys from the plan


class ImportItemOut(BaseModel):
    key: str
    section: Literal["settings", "connections", "service_checks", "pipelines", "alert_channels", "alert_rules"]
    name: str
    action: Literal["create", "update", "skip", "conflict"]
    notes: list[str]


class ImportPlanOut(BaseModel):
    instance: str  # where the file came from
    exported_at: str | None
    version: int
    secrets: str  # none | encrypted
    secrets_used: bool  # a passphrase opened them
    notes: list[str]
    items: list[ImportItemOut]


class ImportResultOut(BaseModel):
    key: str
    section: str
    name: str
    action: str
    ok: bool
    error: str | None
