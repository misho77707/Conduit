"""comparisons: source vs target data diffs (results live on disk, only the latest few are kept)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-07 12:00:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

JsonType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "comparisons",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("phase", sa.String(length=80), nullable=False),
        sa.Column("spec", JsonType, nullable=False),
        sa.Column("summary", JsonType, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("rows_source", sa.BigInteger(), nullable=False),
        sa.Column("rows_target", sa.BigInteger(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("celery_id", sa.String(length=64), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_comparisons_status", "comparisons", ["status"])


def downgrade() -> None:
    op.drop_index("ix_comparisons_status", table_name="comparisons")
    op.drop_table("comparisons")
