"""User management (superadmins only): add, change role, reset password, deactivate / reactivate, delete.

The last active superadmin can't be demoted, deactivated or deleted (409), not even by themselves. Changing a
role, deactivating and resetting a password end the user's sessions (token_version)."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_session
from ..deps import superadmin_only
from ..models import User
from ..schemas import ResetPasswordIn, UserAdminOut, UserCreateIn, UserPatchIn
from ..security import hash_password
from ..users import delete_user, loses_last_superadmin, password_problem, username_problem

router = APIRouter(prefix="/users", tags=["users"])


def _or_404(session: Session, user_id: uuid.UUID) -> User:
    u = session.get(User, user_id)
    if u is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return u


def _out(u: User, names: dict[uuid.UUID, str]) -> UserAdminOut:
    return UserAdminOut(id=u.id, username=u.username, role=u.role, is_active=u.is_active,
                        must_change_password=u.must_change_password, created_at=u.created_at,
                        created_by=names.get(u.created_by_id) if u.created_by_id else None, last_login_at=u.last_login_at)


def _one(session: Session, u: User) -> UserAdminOut:
    creator = session.get(User, u.created_by_id) if u.created_by_id else None
    return _out(u, {creator.id: creator.username} if creator else {})


def _last_superadmin(u: User, what: str) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, f"{u.username} is the last active superadmin, so they can't be {what}: "
                                                   "make another user superadmin first")


@router.get("", dependencies=[Depends(superadmin_only)])
def list_users(session: Session = Depends(get_session)) -> list[UserAdminOut]:
    users = list(session.scalars(select(User).order_by(User.created_at)))
    names = {u.id: u.username for u in users}
    return [_out(u, names) for u in users]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreateIn, session: Session = Depends(get_session),
                me: User = Depends(superadmin_only)) -> UserAdminOut:
    name = body.username.strip()
    problem = username_problem(name) or password_problem(name, body.temporary_password)
    if problem:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, problem)
    taken = HTTPException(status.HTTP_409_CONFLICT, f"There is already a user called {name!r} (usernames ignore case)")
    if session.scalar(select(User.id).where(func.lower(User.username) == name.lower())):
        raise taken
    u = User(username=name, role=body.role, password_hash=hash_password(body.temporary_password),
             must_change_password=True, created_by_id=me.id)
    session.add(u)
    try:
        session.commit()
    except IntegrityError:  # created at the same moment by someone else
        session.rollback()
        raise taken from None
    return _one(session, u)


@router.patch("/{user_id}")
def update_user(user_id: uuid.UUID, body: UserPatchIn, session: Session = Depends(get_session),
                me: User = Depends(superadmin_only)) -> UserAdminOut:
    u = _or_404(session, user_id)
    if loses_last_superadmin(session, u, role=body.role, active=body.is_active):
        raise _last_superadmin(u, "demoted" if body.role not in (None, "superadmin") else "deactivated")
    ends_sessions = (body.role is not None and body.role != u.role) or (body.is_active is False and u.is_active)
    if body.role is not None:
        u.role = body.role
    if body.is_active is not None:
        u.is_active = body.is_active
    if ends_sessions:
        u.token_version += 1
    session.commit()
    return _one(session, u)


@router.post("/{user_id}/reset-password")
def reset_password(user_id: uuid.UUID, body: ResetPasswordIn, session: Session = Depends(get_session),
                   me: User = Depends(superadmin_only)) -> UserAdminOut:
    u = _or_404(session, user_id)
    if u.id == me.id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Use Change password for your own account")
    problem = password_problem(u.username, body.temporary_password)
    if problem:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, problem)
    u.password_hash = hash_password(body.temporary_password)
    u.must_change_password = True
    u.token_version += 1
    session.commit()
    return _one(session, u)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_user(user_id: uuid.UUID, session: Session = Depends(get_session), me: User = Depends(superadmin_only)) -> None:
    u = _or_404(session, user_id)
    if loses_last_superadmin(session, u, deleting=True):
        raise _last_superadmin(u, "deleted")
    delete_user(session, u)
    session.commit()
