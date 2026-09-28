import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..deps import current_user
from ..models import User
from ..schemas import LoginIn, TokenOut, UserOut
from ..security import create_token, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

# Tiny in-memory brute-force guard: 10 failures / 5 min per client address.
_FAILS: dict[str, deque[float]] = defaultdict(deque)
_WINDOW_S, _MAX_FAILS = 300, 10


def _throttled(key: str) -> bool:
    q = _FAILS[key]
    now = time.monotonic()
    while q and now - q[0] > _WINDOW_S:
        q.popleft()
    return len(q) >= _MAX_FAILS


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request, session: Session = Depends(get_session)) -> TokenOut:
    key = request.client.host if request.client else "unknown"
    if _throttled(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed attempts. Try again in a few minutes.")
    user = session.scalar(select(User).where(User.username == body.username))
    # Verify against a dummy hash when the user is unknown so timing does not reveal valid usernames.
    stored = user.password_hash if user else "scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA==$" + "A" * 43 + "="
    ok = verify_password(body.password, stored) and user is not None and user.is_active
    if not ok:
        _FAILS[key].append(time.monotonic())
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    _FAILS.pop(key, None)
    return TokenOut(access_token=create_token(str(user.id), user.role))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> User:
    return user
