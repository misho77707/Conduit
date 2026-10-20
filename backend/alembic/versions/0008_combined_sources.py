"""combined sources: a pipeline's cached source schema, and a job's phase (staging / joining / loading)

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-20 12:00:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

JsonType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("pipelines", sa.Column("source_columns", JsonType, nullable=True))
    op.add_column("jobs", sa.Column("phase", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "phase")
    op.drop_column("pipelines", "source_columns")
