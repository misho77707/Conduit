"""Short rolling log per job (last 200 lines), kept in Redis; falls back to memory when Redis is unreachable."""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from typing import Any

from ..config import get_settings

MAX_LINES = 200
_mem: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_LINES))
_lock = threading.Lock()
_client: Any = None
_client_url: str | None = None
_down_until = 0.0


def _redis():
    global _client, _client_url, _down_until
    if time.monotonic() < _down_until:
        return None
    url = get_settings().redis_url
    if url.startswith("memory://"):
        return None
    if _client is None or _client_url != url:
        try:
            import redis

            _client = redis.Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1)
            _client_url = url
        except Exception:  # noqa: BLE001
            _down_until = time.monotonic() + 30
            return None
    return _client


def _key(job_id: str) -> str:
    return f"conduit:joblog:{job_id}"


def push(job_id: Any, level: str, msg: str) -> None:
    line = json.dumps({"t": time.time(), "level": level, "msg": msg[:500]})
    global _down_until
    r = _redis()
    if r is not None:
        try:
            pipe = r.pipeline()
            pipe.rpush(_key(str(job_id)), line)
            pipe.ltrim(_key(str(job_id)), -MAX_LINES, -1)
            pipe.expire(_key(str(job_id)), 7 * 86400)
            pipe.execute()
            return
        except Exception:  # noqa: BLE001
            _down_until = time.monotonic() + 30
    with _lock:
        _mem[str(job_id)].append(line)


def tail(job_id: Any, n: int = 100) -> list[dict[str, Any]]:
    global _down_until
    r = _redis()
    lines: list[Any] = []
    if r is not None:
        try:
            lines = r.lrange(_key(str(job_id)), -n, -1)
        except Exception:  # noqa: BLE001
            _down_until = time.monotonic() + 30
    if not lines:
        with _lock:
            lines = list(_mem.get(str(job_id), []))[-n:]
    return [json.loads(x) for x in lines]


def clear(job_id: Any) -> None:
    r = _redis()
    if r is not None:
        try:
            r.delete(_key(str(job_id)))
        except Exception:  # noqa: BLE001
            pass
    with _lock:
        _mem.pop(str(job_id), None)
