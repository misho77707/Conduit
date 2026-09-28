"""pipelines: saved read -> transform -> load recipes

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-27 12:00:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

JsonType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "pipelines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("source", JsonType, nullable=False),
        sa.Column("target", JsonType, nullable=False),
        sa.Column("options", JsonType, nullable=False),
        sa.Column("steps", JsonType, nullable=False),
        sa.Column("output_columns", JsonType, nullable=True),
        sa.Column("schedule", sa.String(length=120), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_job_id", sa.Uuid(), nullable=True),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["last_job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_pipelines_name"),
    )
    op.add_column("jobs", sa.Column("pipeline_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_jobs_pipeline_id", "jobs", "pipelines", ["pipeline_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.drop_constraint("fk_jobs_pipeline_id", "jobs", type_="foreignkey")
    op.drop_column("jobs", "pipeline_id")
    op.drop_table("pipelines")
