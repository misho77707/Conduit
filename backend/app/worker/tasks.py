import logging

from ..db import get_sessionmaker
from ..services import all_connections, test_many
from .celery_app import celery_app

log = logging.getLogger("conduit.worker")


@celery_app.task(name="conduit.health.check_all")
def check_all_connections() -> dict:
    """Test every saved connection and overwrite its current state (no history is kept)."""
    with get_sessionmaker()() as session:
        pairs = test_many(session, all_connections(session))
    counts: dict[str, int] = {}
    for _, res in pairs:
        counts[res.status] = counts.get(res.status, 0) + 1
    log.info("health check: %s", counts or "no connections")
    return counts


@celery_app.task(name="conduit.monitor.run_due")
def run_due_service_checks() -> int:
    """Run the service checks whose interval has passed. The result of each is stored on the check (no history)."""
    from ..monitor.checks import run_due

    return run_due()


@celery_app.task(name="conduit.pipelines.run_due")
def run_due_pipelines() -> int:
    """Queue a job for every enabled, previewed pipeline whose cron schedule has come due."""
    from ..engine.schedule import run_due

    return run_due()


@celery_app.task(name="conduit.jobs.run", acks_late=True, reject_on_worker_lost=True)
def run_job_task(job_id: str) -> str:
    """Run (or resume) one load job. Redelivered if the worker dies; the runner continues from saved progress."""
    from ..engine.runner import run_job

    return run_job(job_id)


@celery_app.task(name="conduit.compare.run")
def run_comparison_task(comparison_id: str) -> str:
    """Compare a source with a target and store the differences (latest few comparisons only)."""
    from ..compare.engine import run_comparison

    return run_comparison(comparison_id)
