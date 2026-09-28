"""pipeline scheduling: next_run_at + an optimistic claim, mirroring service_checks

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-27 16:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pipelines", sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("pipelines", sa.Column("schedule_claimed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("pipelines", "schedule_claimed_at")
    op.drop_column("pipelines", "next_run_at")
