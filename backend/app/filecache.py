"""A small on-disk cache under <data>/remote/cache/: files unpacked from .zip archives and remote files (or their
first megabytes) fetched for previews. One folder per entry, keyed by what identifies the content (path + size +
mtime / ETag), so a changed file gets a new entry. Capped by total size (CONDUIT_REMOTE_CACHE_MB); the least
recently used entries go first. Loads never read from here: a job downloads its own files.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
import time
from pathlib import Path

from .config import get_settings

_lock = threading.Lock()


def root() -> Path:
    return Path(get_settings().remote_dir) / "cache"


def entry_dir(key: str) -> Path:
    return root() / hashlib.sha256(key.encode()).hexdigest()[:24]


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name).strip("._") or "file"


def lookup(key: str, name: str) -> Path | None:
    """The cached file for `key`, or None. A hit marks the entry as recently used."""
    p = entry_dir(key) / safe_name(name)
    if p.is_file():
        now = time.time()
        os.utime(p.parent, (now, now))
        return p
    return None


def target(key: str, name: str) -> Path:
    """Where to write a new entry (its folder is created). Write to <path>.part and rename when complete."""
    d = entry_dir(key)
    d.mkdir(parents=True, exist_ok=True)
    return d / safe_name(name)


def evict(keep: Path | None = None) -> None:
    """Drop least recently used entries until the cache fits its cap."""
    cap = get_settings().remote_cache_mb * 1024 * 1024
    with _lock:
        if not root().is_dir():
            return
        entries = []
        for d in root().iterdir():
            if d.is_dir():
                size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                entries.append((d.stat().st_mtime, size, d))
        total = sum(s for _, s, _ in entries)
        for _, size, d in sorted(entries):
            if total <= cap:
                break
            if keep is not None and keep.parent == d:
                continue
            shutil.rmtree(d, ignore_errors=True)
            total -= size
