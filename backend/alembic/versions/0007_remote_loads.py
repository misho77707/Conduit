"""remote intake: files already loaded per source/target pair, and the scheduler's last check on a pipeline

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-08 12:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "remote_loads",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_key", sa.String(length=400), nullable=False),
        sa.Column("target_key", sa.String(length=400), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("path_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=300), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("modified", sa.DateTime(timezone=True), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("loaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_key", "target_key", "path_hash", "version", name="uq_remote_load"),
    )
    op.create_index("ix_remote_loads_pair", "remote_loads", ["source_key", "target_key"])
    op.add_column("pipelines", sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("pipelines", sa.Column("last_check_note", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("pipelines", "last_check_note")
    op.drop_column("pipelines", "last_checked_at")
    op.drop_index("ix_remote_loads_pair", table_name="remote_loads")
    op.drop_table("remote_loads")
