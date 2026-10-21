"""roles: superadmin / admin / viewer, password and session state on users, owner links that survive a user delete

- users: must_change_password, token_version (bumped to end a user's sessions), last_login_at, created_by_id
- every existing "admin" becomes "superadmin"; any other value but the three roles (the never-used "member")
  becomes "viewer"; if that leaves no active superadmin, the oldest active user becomes one
- users.role is constrained to superadmin | admin | viewer
- jobs.trigger: manual | schedule (a scheduled run shows "Started by: schedule")
- the owner_id -> users.id foreign keys of connections, jobs, service_checks, pipelines and comparisons become
  ON DELETE SET NULL, so deleting a user keeps their work
- a single-user install: rows saved without an owner (pipelines never recorded one) are attributed to that user

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-21 10:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

ROLES = ("superadmin", "admin", "viewer")
OWNED = ("connections", "jobs", "service_checks", "pipelines", "comparisons")
# Names for constraints SQLite's batch mode reflects without one (the owner FKs were created unnamed)
NC = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s", "ck": "ck_%(table_name)s_%(constraint_name)s"}

users = sa.table("users", sa.column("id", sa.Uuid()), sa.column("role", sa.String()),
                 sa.column("is_active", sa.Boolean()), sa.column("created_at", sa.DateTime(timezone=True)))


def _owner_fk(bind, table: str) -> str | None:
    for fk in sa.inspect(bind).get_foreign_keys(table):
        if fk["constrained_columns"] == ["owner_id"] and fk["referred_table"] == "users":
            return fk["name"]
    return None


def _owner_fks(ondelete: str | None, name: str) -> None:
    """Re-create every owner_id -> users.id foreign key with this ON DELETE behaviour."""
    bind = op.get_bind()
    for t in OWNED:
        if bind.dialect.name == "sqlite":  # no ALTER of constraints: batch mode copies the table
            with op.batch_alter_table(t, recreate="always", naming_convention=NC) as b:
                b.drop_constraint(_owner_fk(bind, t) or f"fk_{t}_owner_id_users", type_="foreignkey")
                b.create_foreign_key(name.format(t=t), "users", ["owner_id"], ["id"], ondelete=ondelete)
        else:
            old = _owner_fk(bind, t)
            if old:
                op.drop_constraint(old, t, type_="foreignkey")
            op.create_foreign_key(name.format(t=t), t, "users", ["owner_id"], ["id"], ondelete=ondelete)


def upgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table("users", naming_convention=NC) as b:
        b.add_column(sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()))
        b.add_column(sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"))
        b.add_column(sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("created_by_id", sa.Uuid(), nullable=True))
        b.create_foreign_key("fk_users_created_by_id_users", "users", ["created_by_id"], ["id"], ondelete="SET NULL")

    op.execute(users.update().where(users.c.role == "admin").values(role="superadmin"))
    op.execute(users.update().where(users.c.role.notin_(ROLES)).values(role="viewer"))
    active_super = sa.select(sa.func.count()).select_from(users).where(users.c.role == "superadmin", users.c.is_active.is_(True))
    if not bind.scalar(active_super):
        oldest = bind.scalar(sa.select(users.c.id).where(users.c.is_active.is_(True)).order_by(users.c.created_at).limit(1))
        if oldest is not None:
            op.execute(users.update().where(users.c.id == oldest).values(role="superadmin"))
    with op.batch_alter_table("users", naming_convention=NC) as b:
        b.create_check_constraint(op.f("ck_users_role"), sa.column("role").in_(ROLES))

    with op.batch_alter_table("jobs") as b:
        b.add_column(sa.Column("trigger", sa.String(length=20), nullable=False, server_default="manual"))

    _owner_fks("SET NULL", "fk_{t}_owner_id_users")

    if bind.scalar(sa.select(sa.func.count()).select_from(users)) == 1:
        only = bind.scalar(sa.select(users.c.id))
        for t in OWNED:
            tbl = sa.table(t, sa.column("owner_id", sa.Uuid()))
            op.execute(tbl.update().where(tbl.c.owner_id.is_(None)).values(owner_id=only))


def downgrade() -> None:
    _owner_fks(None, "{t}_owner_id_fkey")
    with op.batch_alter_table("jobs") as b:
        b.drop_column("trigger")
    with op.batch_alter_table("users", naming_convention=NC) as b:
        b.drop_constraint("ck_users_role", type_="check")
        b.drop_constraint("fk_users_created_by_id_users", type_="foreignkey")
        b.drop_column("created_by_id")
        b.drop_column("last_login_at")
        b.drop_column("token_version")
        b.drop_column("must_change_password")
    op.execute(users.update().where(users.c.role == "superadmin").values(role="admin"))
