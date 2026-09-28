"""monitor: service checks (current state only)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-27 05:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "service_checks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("target", sa.String(length=500), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=True),
        sa.Column("query", sa.Text(), nullable=True),
        sa.Column("expect", sa.String(length=20), nullable=False),
        sa.Column("interval_s", sa.Integer(), nullable=False),
        sa.Column("verify_tls", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("message", sa.String(length=300), nullable=False),
        sa.Column("latency_ms", sa.Double(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("service_checks")
