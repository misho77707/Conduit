import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_session
from ..deps import self_service
from ..models import User, utcnow
from ..schemas import LoginIn, PasswordChangeIn, TokenOut, UserOut
from ..security import create_token, hash_password, verify_password
from ..users import password_problem

router = APIRouter(prefix="/auth", tags=["auth"])

# Tiny in-memory brute-force guard: 10 failures / 5 min per username and client address, so one person's typos
# never lock out anyone else. The client address is the real one behind the bundled nginx: uvicorn takes it from
# X-Forwarded-For only when the request comes from that nginx (--proxy-headers --forwarded-allow-ips, see
# docker-compose.yml), and nginx sets the header to the address it saw itself.
_FAILS: dict[str, deque[float]] = defaultdict(deque)
_WINDOW_S, _MAX_FAILS = 300, 10


def _key(request: Request, username: str) -> str:
    return f"{request.client.host if request.client else 'unknown'}|{username.strip().lower()}"


def _throttled(key: str) -> bool:
    q = _FAILS[key]
    now = time.monotonic()
    while q and now - q[0] > _WINDOW_S:
        q.popleft()
    return len(q) >= _MAX_FAILS


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request, session: Session = Depends(get_session)) -> TokenOut:
    key = _key(request, body.username)
    if _throttled(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed attempts. Try again in a few minutes.")
    user = session.scalar(select(User).where(User.username == body.username))
    # Verify against a dummy hash when the user is unknown so timing does not reveal valid usernames.
    stored = user.password_hash if user else "scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA==$" + "A" * 43 + "="
    # A deactivated account gets the same answer as a wrong password
    ok = verify_password(body.password, stored) and user is not None and user.is_active
    if not ok:
        _FAILS[key].append(time.monotonic())
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    _FAILS.pop(key, None)
    user.last_login_at = utcnow()
    session.commit()
    return TokenOut(access_token=create_token(str(user.id), user.role, user.token_version))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(self_service)) -> User:
    return user


@router.post("/password", response_model=TokenOut)
def change_password(body: PasswordChangeIn, request: Request, session: Session = Depends(get_session),
                    user: User = Depends(self_service)) -> TokenOut:
    """Change your own password (every role; also the way out of a temporary one). Every other session of yours
    ends; this one continues with the token returned here."""
    key = _key(request, user.username)
    if _throttled(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed attempts. Try again in a few minutes.")
    if not verify_password(body.current_password, user.password_hash):
        _FAILS[key].append(time.monotonic())
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The current password is not correct")
    problem = password_problem(user.username, body.new_password, user.password_hash)
    if problem:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, problem)
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    user.token_version += 1
    session.commit()
    return TokenOut(access_token=create_token(str(user.id), user.role, user.token_version))
