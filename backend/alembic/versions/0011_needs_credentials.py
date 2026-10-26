"""connections: needs_credentials (an imported connection whose secrets did not come with it)

Set by Control & Setup > Import when a connection arrives without its secrets: health checks skip it, and tests,
previews and jobs refuse it, until it is edited with its secrets.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-26 12:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("connections") as b:
        b.add_column(sa.Column("needs_credentials", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("connections") as b:
        b.drop_column("needs_credentials")
