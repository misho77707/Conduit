"""First-run setup: make sure a local admin exists."""
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
        session.add(User(username=s.admin_user, password_hash=hash_password(password), role="admin"))
        session.commit()
    if generated:
        # Shown once. Set CONDUIT_ADMIN_PASSWORD to choose your own.
        log.warning("Created admin user '%s' with generated password: %s", s.admin_user, password)
    else:
        log.info("Created admin user '%s'", s.admin_user)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    seed_admin()
