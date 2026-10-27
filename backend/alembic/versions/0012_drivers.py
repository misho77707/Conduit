"""connections: driver (which driver option moves a connection's data: auto, or an option id)

Existing Postgres and MySQL connections keep the driver they have always used, set explicitly ("psycopg",
"pymysql"), so nothing changes under them; new ones default to "auto" (the fastest available, with fallback).
Types with a single driver keep NULL.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-27 10:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("connections") as b:
        b.add_column(sa.Column("driver", sa.String(20), nullable=True))
    op.execute("UPDATE connections SET driver = 'psycopg' WHERE type = 'postgres'")
    op.execute("UPDATE connections SET driver = 'pymysql' WHERE type = 'mysql'")


def downgrade() -> None:
    with op.batch_alter_table("connections") as b:
        b.drop_column("driver")
