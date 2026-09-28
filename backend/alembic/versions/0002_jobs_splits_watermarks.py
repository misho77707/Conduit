"""load engine: jobs, job splits, watermarks

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-26 21:00:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("spec", _JSON, nullable=False),
        sa.Column("plan", _JSON, nullable=True),
        sa.Column("source_conn_id", sa.Uuid(), nullable=True),
        sa.Column("target_conn_id", sa.Uuid(), nullable=True),
        sa.Column("rows_total", sa.BigInteger(), nullable=True),
        sa.Column("rows_total_exact", sa.Boolean(), nullable=False),
        sa.Column("rows_read", sa.BigInteger(), nullable=False),
        sa.Column("rows_written", sa.BigInteger(), nullable=False),
        sa.Column("rows_rejected", sa.BigInteger(), nullable=False),
        sa.Column("splits_total", sa.Integer(), nullable=False),
        sa.Column("rate_rows_s", sa.Double(), nullable=True),
        sa.Column("workers", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("celery_id", sa.String(length=64), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_jobs_status"), "jobs", ["status"], unique=False)
    op.create_table(
        "job_splits",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=300), nullable=False),
        sa.Column("spec", _JSON, nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("est_rows", sa.BigInteger(), nullable=True),
        sa.Column("rows_read", sa.BigInteger(), nullable=False),
        sa.Column("rows_written", sa.BigInteger(), nullable=False),
        sa.Column("rows_rejected", sa.BigInteger(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("resume", _JSON, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("job_id", "idx"),
    )
    op.create_table(
        "watermarks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_key", sa.String(length=400), nullable=False),
        sa.Column("target_key", sa.String(length=400), nullable=False),
        sa.Column("column", sa.String(length=200), nullable=False),
        sa.Column("value", _JSON, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_key", "target_key", "column", name="uq_watermark_pair"),
    )
    op.create_index("ix_watermarks_pair", "watermarks", ["source_key", "target_key"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_watermarks_pair", table_name="watermarks")
    op.drop_table("watermarks")
    op.drop_table("job_splits")
    op.drop_index(op.f("ix_jobs_status"), table_name="jobs")
    op.drop_table("jobs")
