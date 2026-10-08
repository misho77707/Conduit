"""File-storage connectors (S3, SFTP, HTTP): files, not tables.

They share one small filesystem interface that the remote-intake code uses for browsing, pattern matching,
partial reads and downloads, so the three protocols differ only in how they list, stat and open a file:

    test()  list(path)  glob(pattern)  info(path)  open(path) -> seekable binary file  download(path, dest)

Every path is relative to the connection's base (S3 prefix, SFTP base path, HTTP base URL), uses "/" and may
not climb out of it with "..". A storage connection is never a database: list_tables / inspect raise
NotADatabase, which the API reports as a 422.
"""
from __future__ import annotations

import io
import posixpath
import re
from abc import abstractmethod
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, ClassVar

from .base import Connector, NotADatabase, TableInfo, TableSchema, UserFacingError

CHUNK = 1024 * 1024


class RemoteError(UserFacingError):
    """A storage operation failed in a way worth showing to the user as-is."""


class RemoteNotFound(RemoteError, FileNotFoundError):
    pass


class RangeNotSupported(RemoteError):
    """The server cannot serve a byte range (some HTTP servers): the caller has to download the whole file."""


@dataclass
class RemoteEntry:
    name: str  # last path segment
    path: str  # relative to the connection base, "/"-separated, no leading "/"
    is_dir: bool = False
    size: int | None = None
    modified: datetime | None = None
    etag: str | None = None  # S3 ETag / HTTP ETag; None for SFTP (size + mtime identify a version there)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def version(self) -> str:
        """Identifies this version of the file: changes when the content (very likely) changed."""
        return self.etag or f"{self.size}:{self.modified.isoformat() if self.modified else ''}"


def clean_path(path: str) -> str:
    """Normalize a user path relative to the base: "/"-separated, no leading "/", never escaping with ".."."""
    p = (path or "").replace("\\", "/").strip()
    parts = [s for s in p.split("/") if s not in ("", ".")]
    if any(s == ".." for s in parts):
        raise RemoteError("Paths may not contain '..': they are relative to the connection's base folder")
    return "/".join(parts)


def has_glob(path: str) -> bool:
    return any(ch in path for ch in "*?[")


def glob_regex(pattern: str) -> re.Pattern[str]:
    """`*` and `?` stay within one folder; `**` (a whole segment) spans any number of folders."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        out.append("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def split_glob(pattern: str) -> tuple[str, str]:
    """(static folder before the first wildcard segment, the rest)."""
    segs = pattern.split("/")
    for i, s in enumerate(segs):
        if has_glob(s):
            return "/".join(segs[:i]), "/".join(segs[i:])
    return "/".join(segs[:-1]), segs[-1]


class RangeReader(io.RawIOBase):
    """A seekable read-only file over `read_range(start, length)` (S3 / HTTP byte ranges), with a small block
    cache so the many small reads of a Parquet footer don't each cost a round trip."""

    BLOCK = 256 * 1024

    def __init__(self, size: int, read_range, name: str = "", on_close=None) -> None:
        self.size, self._read_range, self.name = size, read_range, name
        self.pos = 0
        self._blocks: dict[int, bytes] = {}
        self._on_close = on_close

    def close(self) -> None:
        if not self.closed and self._on_close:
            self._on_close()
        super().close()

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence]
        self.pos = max(0, base + offset)
        return self.pos

    def _block(self, n: int) -> bytes:
        if n not in self._blocks:
            if len(self._blocks) > 64:
                self._blocks.pop(next(iter(self._blocks)))
            start = n * self.BLOCK
            self._blocks[n] = self._read_range(start, min(self.BLOCK, self.size - start)) if start < self.size else b""
        return self._blocks[n]

    def readinto(self, b) -> int:
        want = min(len(b), max(0, self.size - self.pos))
        if want <= 0:
            return 0
        if want > 4 * self.BLOCK:  # big sequential read: one request, skip the cache
            data = self._read_range(self.pos, want)
        else:
            out = bytearray()
            while len(out) < want:
                blk = self._block((self.pos + len(out)) // self.BLOCK)
                off = (self.pos + len(out)) % self.BLOCK
                piece = blk[off: off + want - len(out)]
                if not piece:
                    break
                out += piece
            data = bytes(out)
        b[: len(data)] = data
        self.pos += len(data)
        return len(data)


class StorageConnector(Connector):
    category: ClassVar[str] = "storage"
    globbable: ClassVar[bool] = True  # HTTP is not: one URL is one file

    # ---- not a database ------------------------------------------------------------
    def list_tables(self) -> list[TableInfo]:
        raise NotADatabase(f"{self.label} connections hold files, not tables: use it as a remote file source")

    def inspect(self, table: str) -> TableSchema:
        raise NotADatabase(f"{self.label} connections hold files, not tables: use it as a remote file source")

    # ---- filesystem ------------------------------------------------------------------
    @abstractmethod
    def list(self, path: str) -> list[RemoteEntry]:
        """One folder's entries (not recursive), hidden ones (".name") skipped."""

    @abstractmethod
    def info(self, path: str) -> RemoteEntry:
        """One file's size / modified time / ETag. RemoteNotFound when it does not exist."""

    @abstractmethod
    def open(self, path: str) -> BinaryIO:
        """A seekable, read-only binary file. Close it when done."""

    def walk(self, folder: str, recursive: bool, limit: int) -> Iterator[RemoteEntry]:
        """Files under `folder` (and below, when `recursive`). Raises RemoteError after `limit` entries."""
        seen = 0
        stack = [clean_path(folder)]
        while stack:
            for e in self.list(stack.pop()):
                seen += 1
                if seen > limit:
                    raise RemoteError(f"More than {limit:,} entries under '{folder or '/'}': narrow the pattern's folder")
                if e.is_dir:
                    if recursive:
                        stack.append(e.path)
                else:
                    yield e

    def glob(self, pattern: str, limit: int = 100_000) -> list[RemoteEntry]:
        """Files matching a pattern (`*`, `?`, `**`), relative to the base. A plain path matches itself."""
        pattern = clean_path(pattern)
        if not has_glob(pattern):
            try:
                e = self.info(pattern)
            except RemoteNotFound:
                return []
            return [] if e.is_dir else [e]
        if not self.globbable:
            raise RemoteError(f"{self.label} sources are single files: patterns need S3 or SFTP")
        folder, rest = split_glob(pattern)
        rx = glob_regex(pattern)
        recursive = "/" in rest or "**" in rest
        return [e for e in self.walk(folder, recursive, limit) if rx.match(e.path)]

    def stream(self, path: str) -> Iterator[bytes]:
        """The whole file as a sequence of chunks (one request where the protocol allows)."""
        with self.open(path) as src:
            while chunk := src.read(CHUNK):
                yield chunk

    def download(self, path: str, dest: Path, max_bytes: int) -> int:
        """Stream a file to `dest` (via dest.part, renamed when complete). Returns the byte count."""
        part = dest.with_name(dest.name + ".part")
        n = 0
        gen = self.stream(path)
        try:
            with part.open("wb") as out:
                for chunk in gen:
                    n += len(chunk)
                    if n > max_bytes:
                        raise RemoteError(f"{posixpath.basename(path)} is larger than the {max_bytes // 1024 // 1024:,} MB "
                                          "limit (CONDUIT_REMOTE_MAX_FILE_MB)")
                    out.write(chunk)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        finally:
            gen.close()
        part.replace(dest)
        return n

    def display_path(self, path: str) -> str:
        """How a path on this connection is shown to people (in job names, notes, errors)."""
        return f"{self.display_host().rstrip('/')}/{clean_path(path)}"
