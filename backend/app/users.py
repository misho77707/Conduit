"""User accounts: password and username rules, the last-superadmin rule, deleting a user without losing their work."""
import re
import uuid

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import Comparison, Connection, Job, Pipeline, ServiceCheck, User
from .security import verify_password

MIN_PASSWORD, MAX_PASSWORD = 10, 200
USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{1,79}$")
OWNED = (Connection, Job, ServiceCheck, Pipeline, Comparison)  # every table whose rows have an owner_id


def password_problem(username: str, new: str, current_hash: str | None = None) -> str | None:
    """Why `new` can't be this user's password, or None."""
    if len(new) < MIN_PASSWORD:
        return f"The password needs at least {MIN_PASSWORD} characters"
    if len(new) > MAX_PASSWORD:
        return f"The password can have at most {MAX_PASSWORD} characters"
    if new.strip().lower() == username.strip().lower():
        return "The password can't be the username"
    if current_hash and verify_password(new, current_hash):
        return "Choose a password different from the current one"
    return None


def username_problem(username: str) -> str | None:
    if not USERNAME.match(username):
        return "A username has 2 to 80 letters, digits or . _ @ - and starts with a letter or digit"
    return None


def _active_superadmins(session: Session) -> list[uuid.UUID]:
    # FOR UPDATE (PostgreSQL): two superadmins demoting each other at once can't both see the other one still there
    q = select(User.id).where(User.role == "superadmin", User.is_active.is_(True)).with_for_update()
    return list(session.scalars(q))


def loses_last_superadmin(session: Session, user: User, *, role: str | None = None, active: bool | None = None,
                          deleting: bool = False) -> bool:
    """Would this change (a new role, deactivation, deletion) leave no active superadmin?"""
    ids = _active_superadmins(session)
    if user.id not in ids:
        return False
    stays = not deleting and (role or user.role) == "superadmin" and (user.is_active if active is None else active)
    return not stays and len(ids) <= 1


def delete_user(session: Session, user: User) -> None:
    """Their connections, jobs, pipelines, checks and comparisons stay, unowned; so do the users they created."""
    for model in OWNED:
        session.execute(update(model).where(model.owner_id == user.id).values(owner_id=None))
    session.execute(update(User).where(User.created_by_id == user.id).values(created_by_id=None))
    session.delete(user)
