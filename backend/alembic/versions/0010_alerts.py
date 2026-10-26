"""alerts: channels, rules, their current state; check streaks; a small settings table

- alert_channels (Telegram / Slack / email / webhook; secrets Fernet-encrypted; the last send attempt only)
- alert_rules + alert_rule_channels; alert_states: one row per (rule, subject) while something is bad
- connections: err_streak / err_since and bad_streak / bad_since; service_checks: fail_streak / fail_since,
  counted where the check runs, so "down for K consecutive checks" is exact
- settings: key / value (the alert mute now, runtime settings overrides later)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-26 10:00:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

JsonType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "alert_channels",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("config", JsonType, nullable=False),
        sa.Column("secret_enc", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_alert_channels_name"),
    )
    op.create_table(
        "alert_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("event", sa.String(length=30), nullable=False),
        sa.Column("scope", JsonType, nullable=False),
        sa.Column("options", JsonType, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_alert_rules_name"),
    )
    op.create_table(
        "alert_rule_channels",
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("channel_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["rule_id"], ["alert_rules.id"], name="fk_alert_rule_channels_rule", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["channel_id"], ["alert_channels.id"], name="fk_alert_rule_channels_channel", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("rule_id", "channel_id"),
    )
    op.create_table(
        "alert_states",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("subject_kind", sa.String(length=20), nullable=False),
        sa.Column("subject_id", sa.String(length=200), nullable=False),
        sa.Column("subject_label", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("link", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("firing", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("bad_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("notified", sa.String(length=10), nullable=False, server_default="none"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["rule_id"], ["alert_rules.id"], name="fk_alert_states_rule", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_id", "subject_kind", "subject_id", name="uq_alert_state"),
    )
    op.create_index("ix_alert_states_rule_id", "alert_states", ["rule_id"])
    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", JsonType, nullable=True),
        sa.Column("updated_by_id", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], name="fk_settings_updated_by_id_users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("key"),
    )
    with op.batch_alter_table("connections") as b:
        b.add_column(sa.Column("err_streak", sa.Integer(), nullable=False, server_default="0"))
        b.add_column(sa.Column("err_since", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("bad_streak", sa.Integer(), nullable=False, server_default="0"))
        b.add_column(sa.Column("bad_since", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("service_checks") as b:
        b.add_column(sa.Column("fail_streak", sa.Integer(), nullable=False, server_default="0"))
        b.add_column(sa.Column("fail_since", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("service_checks") as b:
        b.drop_column("fail_since")
        b.drop_column("fail_streak")
    with op.batch_alter_table("connections") as b:
        b.drop_column("bad_since")
        b.drop_column("bad_streak")
        b.drop_column("err_since")
        b.drop_column("err_streak")
    op.drop_table("settings")
    op.drop_index("ix_alert_states_rule_id", table_name="alert_states")
    op.drop_table("alert_states")
    op.drop_table("alert_rule_channels")
    op.drop_table("alert_rules")
    op.drop_table("alert_channels")
