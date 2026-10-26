import uuid
from datetime import datetime, timezone

from sqlalchemy import (JSON, BigInteger, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, Table,
                        Text, UniqueConstraint, Uuid)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# JSONB on Postgres, generic JSON elsewhere (tests run on SQLite).
JsonType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


ROLES = ("superadmin", "admin", "viewer")


class User(Base):
    """A local account. Roles (app/deps.py): superadmin (also manages users), admin (everything else), viewer
    (read-only). Authorization always uses the row's role, never the token's claim."""

    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('superadmin', 'admin', 'viewer')", name="ck_users_role"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="viewer")  # superadmin | admin | viewer
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Set by a superadmin's temporary password: nothing but changing it works until the user does
    must_change_password: Mapped[bool] = mapped_column(default=False)
    # In every token; bumped (password change / reset, role change, deactivation) to end the user's sessions
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    connections: Mapped[list["Connection"]] = relationship(back_populates="owner")


class Connection(Base):
    """A saved database. Only the *current* health state lives here: no check history is kept."""

    __tablename__ = "connections"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    type: Mapped[str] = mapped_column(String(30))  # postgres | clickhouse | mysql | sqlite | duckdb | mongodb
    env: Mapped[str | None] = mapped_column(String(30), nullable=True)  # prod | staging | local ...

    # Non-secret settings (host, port, database, user, ssl mode, path, ...).
    config: Mapped[dict] = mapped_column(JsonType, default=dict)
    # What the list shows in "host / path". Credentials are stripped when it is computed.
    display_host: Mapped[str] = mapped_column(String(300), default="")
    # Fernet-encrypted JSON of secret fields (password, connection string, keys).
    secret_enc: Mapped[str] = mapped_column(Text, default="")
    # imported without its secrets: unusable (no health checks, tests, previews or jobs) until edited with them
    needs_credentials: Mapped[bool] = mapped_column(default=False)

    # Current state (overwritten on every check)
    status: Mapped[str] = mapped_column(String(12), default="idle")  # ok | warn | err | idle
    latency_ms: Mapped[float | None] = mapped_column(nullable=True)
    server_version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Consecutive checks that ended `err` (and not `ok`: err or warn), and when that run started: alerts count these
    err_streak: Mapped[int] = mapped_column(Integer, default=0)
    err_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bad_streak: Mapped[int] = mapped_column(Integer, default=0)
    bad_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    owner: Mapped[User | None] = relationship(back_populates="connections")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Job(Base):
    """One transfer or file load. Progress counters live on its splits; the job row holds state and totals."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), default="")
    kind: Mapped[str] = mapped_column(String(20), default="transfer")  # transfer | file_load
    # queued | planning | running | succeeded | failed | cancelled
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    spec: Mapped[dict] = mapped_column(JsonType, default=dict)  # what the user asked for (never holds secrets)
    plan: Mapped[dict | None] = mapped_column(JsonType, nullable=True)  # what the engine decided (columns, DDL, notes)
    source_conn_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    target_conn_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    rows_total: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rows_total_exact: Mapped[bool] = mapped_column(default=False)
    rows_read: Mapped[int] = mapped_column(BigInteger, default=0)  # final values; while running they are summed from splits
    rows_written: Mapped[int] = mapped_column(BigInteger, default=0)
    rows_rejected: Mapped[int] = mapped_column(BigInteger, default=0)
    splits_total: Mapped[int] = mapped_column(Integer, default=0)
    rate_rows_s: Mapped[float | None] = mapped_column(nullable=True)  # smoothed write rate; ETA is computed from it when read
    workers: Mapped[int] = mapped_column(Integer, default=1)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    phase: Mapped[str | None] = mapped_column(String(200), nullable=True)  # a combined job: staging … / joining / loading
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    celery_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    pipeline_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pipelines.id", ondelete="SET NULL"), nullable=True)
    trigger: Mapped[str] = mapped_column(String(20), default="manual")  # manual | schedule ("Started by: schedule")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    splits: Mapped[list["JobSplit"]] = relationship(back_populates="job", cascade="all, delete-orphan", order_by="JobSplit.idx")


class JobSplit(Base):
    """A slice of the source that is read and written independently (and can be retried or resumed on its own)."""

    __tablename__ = "job_splits"

    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(300), default="")
    spec: Mapped[dict] = mapped_column(JsonType, default=dict)  # SplitSpec.to_json()
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending | running | done | failed | cancelled
    est_rows: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rows_read: Mapped[int] = mapped_column(BigInteger, default=0)
    rows_written: Mapped[int] = mapped_column(BigInteger, default=0)
    rows_rejected: Mapped[int] = mapped_column(BigInteger, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    resume: Mapped[dict | None] = mapped_column(JsonType, nullable=True)  # enc()-encoded key of the last committed row
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped[Job] = relationship(back_populates="splits")


class Watermark(Base):
    """Highest value of a column already loaded for a source/target pair: 'load only what is new' next time."""

    __tablename__ = "watermarks"
    __table_args__ = (
        UniqueConstraint("source_key", "target_key", "column", name="uq_watermark_pair"),
        Index("ix_watermarks_pair", "source_key", "target_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_key: Mapped[str] = mapped_column(String(400))  # "<connection id>:<table>"
    target_key: Mapped[str] = mapped_column(String(400))
    column: Mapped[str] = mapped_column(String(200))
    value: Mapped[dict] = mapped_column(JsonType)  # enc()-encoded
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ServiceCheck(Base):
    """An external thing to watch (HTTP endpoint, TCP port, SQL query). Only the latest result is kept: no history."""

    __tablename__ = "service_checks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(10))  # http | tcp | query
    target: Mapped[str] = mapped_column(String(500), default="")  # URL, or host:port (unused for query)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)  # query checks run on this connection
    query: Mapped[str | None] = mapped_column(Text, nullable=True)
    # http: expected status code; tcp: slowest acceptable connect time in ms; query: expected row count. Empty = default.
    expect: Mapped[str] = mapped_column(String(20), default="")
    interval_s: Mapped[int] = mapped_column(Integer, default=30)
    verify_tls: Mapped[bool] = mapped_column(default=True)
    enabled: Mapped[bool] = mapped_column(default=True)

    # Current state (overwritten on every run)
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending | up | warn | down
    message: Mapped[str] = mapped_column(String(300), default="")  # "200 OK", "connection refused" ...
    latency_ms: Mapped[float | None] = mapped_column(nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # a run is in progress
    fail_streak: Mapped[int] = mapped_column(Integer, default=0)  # consecutive `down` runs (alerts count them)
    fail_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Pipeline(Base):
    """A saved read -> transform -> load recipe: the same job engine, run again by name or on a schedule."""

    __tablename__ = "pipelines"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(500), default="")

    source: Mapped[dict] = mapped_column(JsonType, default=dict)  # SourceIn.model_dump()
    target: Mapped[dict] = mapped_column(JsonType, default=dict)  # TargetIn.model_dump()
    options: Mapped[dict] = mapped_column(JsonType, default=dict)  # OptionsIn.model_dump()
    steps: Mapped[list] = mapped_column(JsonType, default=list)  # [{id, kind, config}, ...], read -> load order
    # Cached column list the step chain produced, from the last preview against real data ([{name, type,
    # nullable, source_name}, ...]); a run needs this to know what the "Load" step is writing. Cleared
    # whenever source/steps change, so a stale schema can never be run against.
    output_columns: Mapped[list | None] = mapped_column(JsonType, nullable=True)
    # A combined source's own output (the join SQL's columns) from the last full preview: launching a run never
    # stages the inputs, so this is what it plans with. Cleared together with output_columns.
    source_columns: Mapped[list | None] = mapped_column(JsonType, nullable=True)

    schedule: Mapped[str | None] = mapped_column(String(120), nullable=True)  # cron expression (croniter syntax) or a
    # shortcut like "@daily"; validated on save
    enabled: Mapped[bool] = mapped_column(default=True)  # a disabled pipeline's schedule never fires
    # When the schedule should next fire; null if there is no schedule, the pipeline is disabled, or the cron
    # string went stale. Recomputed whenever `schedule`/`enabled` change, and after every scheduled firing.
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    schedule_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # a
    # scheduled run is in progress (same optimistic-claim pattern as ServiceCheck.claimed_at)
    last_job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    # The scheduler's last look at a remote "newest / all" source that had nothing new (no job is created then,
    # so these are the only trace of it): when, and what it found ("no new files matching ...").
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_check_note: Mapped[str | None] = mapped_column(String(500), nullable=True)

    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RemoteLoad(Base):
    """A remote file already loaded into a target, so a "newest" / "all" run with skip-loaded leaves it out.

    Written when the file's split finishes (its rows are committed in the target then), not when the whole job
    succeeds. `path` is the full resolved location (s3://bucket/key, sftp://host:port/abs/path, URL without its
    query string) so editing a connection's prefix or base path never makes old files look new.
    """

    __tablename__ = "remote_loads"
    __table_args__ = (
        UniqueConstraint("source_key", "target_key", "path_hash", "version", name="uq_remote_load"),
        Index("ix_remote_loads_pair", "source_key", "target_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_key: Mapped[str] = mapped_column(String(400))  # "remote:<connection id>" or "remote:url"
    target_key: Mapped[str] = mapped_column(String(400))  # "<connection id>:<table>"
    path: Mapped[str] = mapped_column(Text)
    path_hash: Mapped[str] = mapped_column(String(64))  # sha256(path): S3 keys alone can be 1024 bytes
    version: Mapped[str] = mapped_column(String(300))  # ETag, or size:mtime (SFTP)
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    modified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Comparison(Base):
    """One source-vs-target data comparison. Result rows live in <data>/compare/<id>/results.duckdb, not here."""

    __tablename__ = "comparisons"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued | running | succeeded | failed | cancelled
    phase: Mapped[str] = mapped_column(String(80), default="")  # what it is doing right now, shown while running
    spec: Mapped[dict] = mapped_column(JsonType, default=dict)  # compare.spec.CompareIn
    summary: Mapped[dict | None] = mapped_column(JsonType, nullable=True)  # counts, columns, keys, notes
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    rows_source: Mapped[int] = mapped_column(BigInteger, default=0)  # rows read so far
    rows_target: Mapped[int] = mapped_column(BigInteger, default=0)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    celery_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)  # the load it verifies, when started from a job
    owner_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---- alerts (Control & Setup): current state only, no send history -------------------------------------

alert_rule_channels = Table(
    "alert_rule_channels", Base.metadata,
    Column("rule_id", Uuid, ForeignKey("alert_rules.id", ondelete="CASCADE"), primary_key=True),
    Column("channel_id", Uuid, ForeignKey("alert_channels.id", ondelete="CASCADE"), primary_key=True),
)


class AlertChannel(Base):
    """Where alerts go: Telegram, Slack, email or a webhook. Only the last send attempt's result is kept."""

    __tablename__ = "alert_channels"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[str] = mapped_column(String(20))  # telegram | slack | email | webhook
    config: Mapped[dict] = mapped_column(JsonType, default=dict)  # non-secret settings (chat id, SMTP host, ...)
    secret_enc: Mapped[str] = mapped_column(Text, default="")  # Fernet-encrypted JSON (token, webhook URL, password)
    enabled: Mapped[bool] = mapped_column(default=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    rules: Mapped[list["AlertRule"]] = relationship(secondary=alert_rule_channels, back_populates="channels")


class AlertRule(Base):
    """When to alert: one event, a scope (everything or picked subjects), thresholds, the channels to send to."""

    __tablename__ = "alert_rules"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    event: Mapped[str] = mapped_column(String(30))  # see app/alerts/catalog.py
    scope: Mapped[dict] = mapped_column(JsonType, default=dict)  # {"all": true} or {"pipelines": [...], ...}
    options: Mapped[dict] = mapped_column(JsonType, default=dict)  # thresholds of the event
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    channels: Mapped[list[AlertChannel]] = relationship(secondary=alert_rule_channels, back_populates="rules",
                                                        lazy="selectin")


class AlertState(Base):
    """One (rule, subject) that is bad right now: pending (not long enough yet), firing, or recovered while alerts
    were muted after its break message went out. Deleted once nothing is left to remember: no history."""

    __tablename__ = "alert_states"
    __table_args__ = (UniqueConstraint("rule_id", "subject_kind", "subject_id", name="uq_alert_state"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("alert_rules.id", ondelete="CASCADE"), index=True)
    subject_kind: Mapped[str] = mapped_column(String(20))  # pipeline | job | connection | check | host
    subject_id: Mapped[str] = mapped_column(String(200))
    subject_label: Mapped[str] = mapped_column(String(300), default="")
    link: Mapped[str] = mapped_column(String(300), default="")  # the app page, e.g. "#/jobs/<id>"
    firing: Mapped[bool] = mapped_column(default=False)
    bad_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # the condition began
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str] = mapped_column(String(500), default="")
    notified: Mapped[str] = mapped_column(String(10), default="none")  # none | firing: what the last message said
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Setting(Base):
    """Small key / value store: the alert mute today, the runtime settings overrides later."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JsonType, nullable=True)
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
