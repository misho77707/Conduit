"""Getting alert messages to channels without ever slowing down or failing what triggered them.

A send runs in the Celery task conduit.alerts.send (a timeout per attempt, two retries); with
CONDUIT_JOB_RUNNER=thread (local dev) on a background thread. Only the outcome is kept, on the channel:
last_sent_at after a success, last_error after a failure - no send history.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

from sqlalchemy import update

from ..config import get_settings
from ..db import get_sessionmaker
from ..models import AlertChannel, utcnow
from ..security import decrypt_secrets
from . import senders
from .messages import Message

log = logging.getLogger("conduit.alerts")

INLINE = False  # tests: send in the calling thread, at once, without retry delays
RETRIES, RETRY_DELAY_S = 2, 10


def channel_secrets(ch: AlertChannel) -> dict[str, Any]:
    return decrypt_secrets(ch.secret_enc) if ch.secret_enc else {}


def record(channel_id: uuid.UUID, error: str | None) -> None:
    values = {"last_error": error[:500]} if error else {"last_error": None, "last_sent_at": utcnow()}
    with get_sessionmaker()() as s:
        s.execute(update(AlertChannel).where(AlertChannel.id == channel_id).values(**values))
        s.commit()


def attempt(channel_id: uuid.UUID | str, payload: dict[str, Any]) -> str | None:
    """One try at one channel; records the outcome. Returns the error, or None (sent, or the channel is gone / off)."""
    cid = uuid.UUID(str(channel_id))
    with get_sessionmaker()() as s:
        ch = s.get(AlertChannel, cid)
        if ch is None or not ch.enabled:
            return None
        kind, config, secrets = ch.kind, dict(ch.config or {}), channel_secrets(ch)
    try:
        senders.send(kind, config, secrets, Message.from_dict(payload))
        err = None
    except senders.SendError as e:
        err = str(e)
    except Exception as e:  # noqa: BLE001 - a bug in a sender must never travel further than this
        log.exception("alert send to channel %s crashed", cid)
        err = f"{type(e).__name__}: could not send"
    record(cid, err)
    return err


def _with_retries(channel_id: uuid.UUID, payload: dict[str, Any], delay: float) -> None:
    for i in range(RETRIES + 1):
        if attempt(channel_id, payload) is None:
            return
        if i < RETRIES and delay:
            time.sleep(delay)


def enqueue(items: list[tuple[uuid.UUID, Message]]) -> None:
    """Send these (channel, message) pairs in the background. Never raises."""
    if not items:
        return
    try:
        if INLINE:
            for cid, m in items:
                _with_retries(cid, m.to_dict(), 0)
            return
        if get_settings().job_runner == "thread":
            threading.Thread(target=lambda: [_with_retries(c, m.to_dict(), RETRY_DELAY_S) for c, m in items],
                             daemon=True, name="conduit-alerts").start()
            return
        from ..worker.tasks import send_alert_task

        for cid, m in items:
            try:
                send_alert_task.apply_async(args=[str(cid), m.to_dict()], retry=False)
            except Exception as e:  # noqa: BLE001 - the broker is down: say so on the channel
                record(cid, f"Could not queue the alert: {type(e).__name__}")
    except Exception:  # noqa: BLE001
        log.exception("queueing alerts failed")
