from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CONDUIT_", env_file=".env", extra="ignore")

    # App storage (connections, users, job state)
    database_url: str = "postgresql+psycopg://conduit:conduit@localhost:5432/conduit"
    redis_url: str = "redis://localhost:6379/0"

    # Master key for encrypting saved connection credentials (Fernet, urlsafe base64, 32 bytes).
    # Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    secret_key: str = ""
    # Signs login tokens. Falls back to a value derived from secret_key when empty.
    jwt_secret: str = ""
    jwt_ttl_minutes: int = 12 * 60

    # Single local admin, created on first start when no users exist.
    admin_user: str = "admin"
    admin_password: str = ""

    # Where uploads, rejects and file-based databases (SQLite/DuckDB) may live.
    # File-based connections must resolve inside one of these roots.
    data_dir: str = "/data"
    allowed_file_roots: str = ""  # comma separated; defaults to data_dir

    # Background health checks (current state only, no history is kept).
    health_check_interval_s: int = 30
    connect_timeout_s: int = 8

    cors_origins: str = "http://localhost:5173"

    # Load engine
    job_runner: str = "celery"  # celery | thread (thread: run jobs inside the API process; for local dev and tests)
    keep_finished_jobs: int = 10  # only the latest finished jobs are kept: no history
    max_workers_per_job: int = 32

    # Monitor
    monitor_disks: str = ""  # extra disks to show: "path[:label],path[:label]" (paths as seen inside the container)
    monitor_warn_pct: int = 85  # CPU / memory / disk level above which the monitor says "High"
    check_timeout_s: int = 8  # HTTP / TCP / docker service checks give up after this long
    docker_socket: str = "/var/run/docker.sock"  # "docker" checks read container state from here (mount read-only)

    @property
    def file_roots(self) -> list[str]:
        raw = self.allowed_file_roots or self.data_dir
        return [r.strip() for r in raw.split(",") if r.strip()]

    @property
    def rejects_dir(self) -> str:
        return f"{self.data_dir.rstrip('/')}/rejects"

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
