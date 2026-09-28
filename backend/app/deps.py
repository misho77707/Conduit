import uuid

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .db import get_session
from .models import User
from .security import decode_token

_bearer = HTTPBearer(auto_error=False)


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: Session = Depends(get_session),
) -> User:
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated",
                                 headers={"WWW-Authenticate": "Bearer"})
    if creds is None:
        raise unauthorized
    payload = decode_token(creds.credentials)
    if not payload:
        raise unauthorized
    try:
        user = session.get(User, uuid.UUID(payload["sub"]))
    except (KeyError, ValueError):
        raise unauthorized from None
    if user is None or not user.is_active:
        raise unauthorized
    return user


def current_user_sse(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    token: str | None = Query(None),
    session: Session = Depends(get_session),
) -> User:
    """Like current_user, but also accepts ?token=... because browsers' EventSource cannot send headers."""
    raw = creds.credentials if creds else token
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    payload = decode_token(raw) if raw else None
    if not payload:
        raise unauthorized
    try:
        user = session.get(User, uuid.UUID(payload["sub"]))
    except (KeyError, ValueError):
        raise unauthorized from None
    if user is None or not user.is_active:
        raise unauthorized
    return user
