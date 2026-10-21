"""Who is calling, and may they call this route.

Every /api route (but login and health) declares exactly one permission, as a dependency:

    self_service      any signed-in user, even one who must still change their password (/auth/me, /auth/password)
    viewer_ok         every role (viewer, admin, superadmin); the route trims what a viewer sees itself
    admin_only        admin and superadmin
    superadmin_only   superadmin (user management)

`viewer_ok_sse` is viewer_ok for EventSource streams (the token comes as ?token=...). tests/test_permissions.py fails
for a route that declares none, or more than one. Roles are always read from the users table, never from the
token's claim; a token is only good while its `ver` matches the user's token_version and the user is active.
"""
import uuid
from collections.abc import Callable

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .db import get_session
from .models import User
from .security import decode_token

_bearer = HTTPBearer(auto_error=False)
RANK = {"viewer": 0, "admin": 1, "superadmin": 2}
PASSWORD_CHANGE_REQUIRED = "password_change_required"


def _user_from_token(raw: str | None, session: Session) -> User:
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    payload = decode_token(raw) if raw else None
    if not payload:
        raise unauthorized
    try:
        user = session.get(User, uuid.UUID(payload["sub"]))
    except (KeyError, ValueError):
        raise unauthorized from None
    if user is None or not user.is_active or int(payload.get("ver", 0)) != user.token_version:
        raise unauthorized  # deleted, deactivated, or the session was ended (password / role change)
    return user


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
                 session: Session = Depends(get_session)) -> User:
    """The signed-in user (no permission check: routes use one of the permissions below)."""
    return _user_from_token(creds.credentials if creds else None, session)


def current_user_sse(creds: HTTPAuthorizationCredentials | None = Depends(_bearer), token: str | None = Query(None),
                     session: Session = Depends(get_session)) -> User:
    """Like current_user, but also accepts ?token=... because browsers' EventSource cannot send headers."""
    return _user_from_token(creds.credentials if creds else token, session)


def forbidden(message: str, code: str = "forbidden") -> HTTPException:
    return HTTPException(status.HTTP_403_FORBIDDEN, {"code": code, "message": message})


def _permission(name: str, least: str, *, sse: bool = False, pending_ok: bool = False) -> Callable[..., User]:
    source = current_user_sse if sse else current_user

    def check(user: User = Depends(source)) -> User:
        if user.must_change_password and not pending_ok:
            raise forbidden("Set a new password first (your password was set by an administrator)", PASSWORD_CHANGE_REQUIRED)
        if RANK.get(user.role, -1) < RANK[least]:
            raise forbidden({"admin": "This needs an admin or superadmin account",
                             "superadmin": "Only a superadmin can manage users"}.get(least, "Not allowed"))
        return user

    check.__name__ = name
    check.permission = name  # what tests/test_permissions.py looks for
    return check


self_service = _permission("self_service", "viewer", pending_ok=True)
viewer_ok = _permission("viewer_ok", "viewer")
viewer_ok_sse = _permission("viewer_ok_sse", "viewer", sse=True)
admin_only = _permission("admin_only", "admin")
superadmin_only = _permission("superadmin_only", "superadmin")


def is_viewer(user: User) -> bool:
    """Read routes open to viewers use this to return the reduced shape."""
    return RANK.get(user.role, -1) < RANK["admin"]
