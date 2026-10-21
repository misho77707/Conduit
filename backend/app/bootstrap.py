"""First-run setup: an empty users table gets one superadmin (CONDUIT_ADMIN_USER / CONDUIT_ADMIN_PASSWORD)."""
import logging
import secrets

from sqlalchemy import func, select

from .config import get_settings
from .db import get_sessionmaker
from .models import User
from .security import hash_password

log = logging.getLogger("conduit.bootstrap")


def seed_admin() -> None:
    s = get_settings()
    with get_sessionmaker()() as session:
        if session.scalar(select(func.count()).select_from(User)):
            return
        password = s.admin_password
        generated = not password
        if generated:
            password = secrets.token_urlsafe(12)
        # A generated password is only ever shown in this log: it is temporary, the first sign-in replaces it
        session.add(User(username=s.admin_user, password_hash=hash_password(password), role="superadmin",
                         must_change_password=generated))
        session.commit()
    if generated:
        # Shown once. Set CONDUIT_ADMIN_PASSWORD to choose your own.
        log.warning("Created superadmin '%s' with a temporary generated password (set a new one at first sign-in): %s",
                    s.admin_user, password)
    else:
        log.info("Created superadmin '%s'", s.admin_user)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    seed_admin()
