"""File-based connections (SQLite, DuckDB) may only touch paths inside the allowed roots."""
from pathlib import Path

from ..config import get_settings


class PathNotAllowed(ValueError):
    pass


def resolve_allowed(path: str) -> Path:
    if not path or not path.strip():
        raise PathNotAllowed("Path is required")
    p = Path(path.strip()).expanduser().resolve()
    for root in get_settings().file_roots:
        if p.is_relative_to(Path(root).resolve()):
            return p
    roots = ", ".join(get_settings().file_roots)
    raise PathNotAllowed(f"Path must be inside an allowed folder ({roots})")
