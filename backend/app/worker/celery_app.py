from datetime import timedelta

from celery import Celery

from ..config import get_settings

s = get_settings()

celery_app = Celery("conduit", broker=s.redis_url, backend=s.redis_url, include=["app.worker.tasks"])
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    task_acks_late=True,  # a killed worker's task is redelivered
    worker_prefetch_multiplier=1,  # long jobs must not queue behind each other on one worker
    result_expires=3600,
    beat_schedule={
        "connection-health-checks": {
            "task": "conduit.health.check_all",
            "schedule": timedelta(seconds=s.health_check_interval_s),
            "options": {"expires": s.health_check_interval_s},  # drop a stale run instead of stacking
        },
        "service-checks-due": {
            "task": "conduit.monitor.run_due",
            "schedule": timedelta(seconds=5),  # each check has its own interval (10 s and up); this only looks what is due
            "options": {"expires": 5},
        },
        "alerts-evaluate": {
            "task": "conduit.alerts.evaluate",
            "schedule": timedelta(seconds=30),  # connections, checks, host, stalled jobs, stale inputs; mute ends
            "options": {"expires": 30},
        },
        "pipelines-due": {
            "task": "conduit.pipelines.run_due",
            "schedule": timedelta(seconds=30),  # cron schedules are minute-grained at best; no need to poll faster
            "options": {"expires": 30},
        },
    },
)
