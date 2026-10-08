import logging
from contextlib import asynccontextmanager

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from .api import auth, compare, connections, files, jobs, monitor, pipelines, remote
from .bootstrap import seed_admin
from .config import get_settings
from .db import get_engine

log = logging.getLogger("conduit")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    if not s.secret_key:
        log.error("CONDUIT_SECRET_KEY is not set: saving connections will fail until it is.")
    else:
        try:
            Fernet(s.secret_key.encode())
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                "CONDUIT_SECRET_KEY is not a valid Fernet key (32 url-safe base64 bytes). Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"") from e
    try:
        seed_admin()
    except Exception:
        log.exception("Could not seed admin user (has `alembic upgrade head` been run?)")
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Conduit", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=get_settings().cors_list, allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
    )
    app.include_router(auth.router, prefix="/api")
    app.include_router(connections.router, prefix="/api")
    app.include_router(connections.meta_router, prefix="/api")
    app.include_router(files.router, prefix="/api")
    app.include_router(jobs.router, prefix="/api")
    app.include_router(monitor.router, prefix="/api")
    app.include_router(pipelines.router, prefix="/api")
    app.include_router(compare.router, prefix="/api")
    app.include_router(remote.router, prefix="/api")

    @app.get("/api/health", tags=["meta"])
    def health() -> dict:
        """Liveness + app database check (used by Docker healthchecks)."""
        with get_engine().connect() as c:
            c.execute(text("SELECT 1"))
        return {"status": "ok"}

    return app


app = create_app()
