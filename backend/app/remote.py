"""Remote intake: files on S3 / SFTP / behind a URL as a source, through the same file reader as local files.

Three ways to choose files (RemoteIn.match): one `exact` file, the `newest` file matching a pattern, or `all`
matching files (one job, one split per file). With skip-loaded, files already loaded into the same target are
left out; a file is recorded (RemoteLoad) as soon as its split finishes, i.e. once its rows are committed.

Where the bytes go:
  - loads download every chosen file first, into <data>/remote/jobs/<job id>/ with a manifest.json; a retry
    reuses the manifest and the files (never re-resolving "newest" / "all"), and the folder is deleted as soon as
    the job succeeds (failed / cancelled jobs keep it for Retry until they are pruned or deleted);
  - Compare downloads a remote side into its own work folder (deleted after the diff);
  - previews (wizard inspect, /jobs/plan, pipeline preview, Compare's column screen) never download a big file:
    small files whole, big text files from their head, big Parquet files via footer + the first rows, all cached
    under <data>/remote/cache (filecache.py, LRU, size-capped).

Nothing here ever shows a URL's query string (pre-signed links carry tokens there): see http.mask_url.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import shutil
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import filecache
from .config import get_settings
from .connectors import category_of
from .connectors.http import HTTPConnector, mask_text, mask_url
from .connectors.storage import RangeNotSupported, RemoteEntry, RemoteError, StorageConnector, has_glob
from .db import get_sessionmaker
from .engine import joblog
from .engine.filesource import FileAdapter, FileSpec, MultiFileAdapter, SourceFile
from .engine.spec import RemoteIn
from .files import FileInspection, UnsupportedFormat, format_from_name, inspect_local, split_compression
from .models import Connection, Job, RemoteLoad, utcnow
from .services import build_connector

HEAD_BYTES = 8 * 1024 * 1024  # a big text file is previewed from its first 8 MB
BIG_ROW_GROUP = 64 * 1024 * 1024  # a Parquet row group above this is never read whole for a preview
LIST_CAP = 200  # files listed in a match summary
ACTIVE = ("queued", "planning", "running")


class NothingToLoad(RemoteError):
    """Every matching file is already loaded (or nothing matches): the run loads 0 rows."""


# ---- connections and keys ----------------------------------------------------------------

def storage(session: Session, connection_id: uuid.UUID | None) -> tuple[StorageConnector, Connection | None]:
    """The storage connector for a remote source (an ad-hoc URL when there is no connection)."""
    timeout = get_settings().remote_timeout_s
    if connection_id is None:
        return HTTPConnector({"base_url": ""}, {}, timeout=min(timeout, 30)), None
    conn = session.get(Connection, connection_id)
    if conn is None:
        raise RemoteError("The remote file's connection no longer exists")
    if category_of(conn.type) != "storage":
        raise RemoteError(f"{conn.name} is a database connection, not file storage (S3 / SFTP / HTTP)")
    c = build_connector(conn)
    if not isinstance(c, StorageConnector):
        raise RemoteError(f"{conn.name} cannot serve files")
    return c, conn


def remember(session: Session, conn: Connection | None, c: StorageConnector) -> None:
    """Store what a connection learned while reading (an SFTP host key seen for the first time)."""
    if conn is not None and c.learned:
        conn.config = {**(conn.config or {}), **c.learned}
        session.commit()
        c.learned = {}


def source_key(remote: RemoteIn | dict) -> str:
    cid = remote.connection_id if isinstance(remote, RemoteIn) else remote.get("connection_id")
    return f"remote:{cid or 'url'}"


def target_key(target: Any) -> str:
    cid, table = (target.connection_id, target.table) if not isinstance(target, dict) else (target.get("connection_id"), target.get("table"))
    return f"{cid}:{table}"


def effective_skip(remote: RemoteIn, target_mode: str | None) -> tuple[bool, str | None]:
    """(skip already-loaded files?, why not when asked to). Exact files are never skipped; with Replace + all
    matching files every run reloads everything (else each run would empty the target and keep only new files)."""
    if remote.match == "exact" or not remote.skip_loaded:
        return False, None
    if remote.match == "all" and target_mode == "replace":
        return False, ("Replace + all matching files: every run replaces the target with every matching file, so "
                       "'skip files already loaded' is ignored.")
    return True, None


def path_hash(full: str) -> str:
    return hashlib.sha256(full.encode()).hexdigest()


def loaded_versions(session: Session, skey: str, tkey: str) -> dict[str, set[str]]:
    """path_hash -> versions already loaded into this target from this source."""
    out: dict[str, set[str]] = {}
    for h, v in session.execute(select(RemoteLoad.path_hash, RemoteLoad.version)
                                .where(RemoteLoad.source_key == skey, RemoteLoad.target_key == tkey)):
        out.setdefault(h, set()).add(v)
    return out


def status_of(e: RemoteEntry, loaded: dict[str, set[str]]) -> str:
    """new (never loaded) | changed (loaded before, overwritten in place since) | loaded (this exact version)."""
    versions = loaded.get(path_hash(e.full))
    if not versions:
        return "new"
    return "loaded" if e.version in versions else "changed"


def record_load(skey: str, tkey: str, rec: dict[str, Any], job_id: uuid.UUID) -> None:
    """Mark one downloaded file as loaded into the target (its split finished: its rows are committed)."""
    with get_sessionmaker()() as s:
        s.add(RemoteLoad(source_key=skey, target_key=tkey, path=rec["full"], path_hash=path_hash(rec["full"]),
                         version=rec["version"][:300], size=rec.get("size"),
                         modified=datetime.fromisoformat(rec["modified"]) if rec.get("modified") else None, job_id=job_id))
        try:
            s.commit()
        except IntegrityError:  # loaded before (e.g. Replace runs, or a retried file): already recorded
            s.rollback()


# ---- matching ------------------------------------------------------------------------------

@dataclass
class Match:
    pattern: str
    match: str
    matches: list[RemoteEntry]  # every matching file (all: by name; newest: newest first)
    chosen: list[RemoteEntry]  # what a run would load now
    status: dict[str, str] = field(default_factory=dict)  # full -> new | changed | loaded (when the target is known)
    skip: bool = False
    notes: list[str] = field(default_factory=list)
    more_left: int = 0  # new files beyond CONDUIT_REMOTE_MAX_FILES, left for the next run
    unsupported: int = 0

    def summary(self, c: StorageConnector) -> dict[str, Any]:
        def item(e: RemoteEntry) -> dict[str, Any]:
            fmt, comp = _name_format(e.name)
            return {"name": e.name, "path": e.path, "size": e.size, "modified": e.modified, "format": fmt,
                    "compression": comp, "status": self.status.get(e.full)}
        counts = {k: sum(1 for v in self.status.values() if v == k) for k in ("new", "changed", "loaded")}
        return {
            "pattern": c.display_path(self.pattern) if not has_glob(self.pattern) else self.pattern, "match": self.match,
            "total": len(self.matches), "total_bytes": sum(e.size or 0 for e in self.matches),
            "files": [item(e) for e in self.matches[:LIST_CAP]], "chosen": [e.path for e in self.chosen],
            "chosen_bytes": sum(e.size or 0 for e in self.chosen),
            "new_count": counts["new"], "changed_count": counts["changed"], "loaded_count": counts["loaded"],
            "known_status": bool(self.status), "skip": self.skip, "more_left": self.more_left,
            "unsupported": self.unsupported, "notes": self.notes,
        }


def _name_format(name: str) -> tuple[str | None, str | None]:
    try:
        return format_from_name(name)
    except UnsupportedFormat:
        return None, None


def resolve_match(c: StorageConnector, remote: RemoteIn, loaded: dict[str, set[str]] | None, skip: bool) -> Match:
    """What `remote` matches right now, and which files a run would load (`skip` leaves out loaded ones)."""
    st = get_settings()
    if remote.match == "exact":
        e = c.info(remote.path)
        if e.is_dir:
            raise RemoteError(f"{c.display_path(remote.path)} is a folder: choose a file, or a pattern like folder/*.csv")
        m = Match(remote.path, "exact", [e], [e])
        if loaded is not None:
            m.status = {e.full: status_of(e, loaded)}
        return m
    found = c.glob(remote.path, limit=st.remote_list_max)
    supported = found if remote.format else [e for e in found if _name_format(e.name)[0] or _name_format(e.name)[1] == "zip"]
    m = Match(remote.path, remote.match, [], [], unsupported=len(found) - len(supported))
    if m.unsupported:
        m.notes.append(f"{m.unsupported} matching file(s) left out: not CSV / TSV / JSON / Parquet / Excel (or .gz / .zip of them)")
    if remote.match == "newest":  # most recently modified first; ties: name, descending
        m.matches = sorted(supported, key=lambda e: (e.modified.timestamp() if e.modified else float("-inf"), e.name), reverse=True)
    else:
        m.matches = sorted(supported, key=lambda e: e.path)
    if loaded is not None:
        m.status = {e.full: status_of(e, loaded) for e in m.matches}
    m.skip = skip and loaded is not None
    if not m.matches:
        m.notes.append(f"No files match {remote.path}")
        return m
    if remote.match == "newest":
        newest = m.matches[0]
        if m.skip and m.status.get(newest.full) == "loaded":
            m.notes.append(f"No new files matching {remote.path}: the newest one, {newest.path}, is already loaded into this target")
        else:
            m.chosen = [newest]
        return m
    todo = [e for e in m.matches if not m.skip or m.status.get(e.full) != "loaded"]
    if m.skip and not todo:
        m.notes.append(f"No new files matching {remote.path} ({len(m.matches)} already loaded into this target)")
    if len(todo) > st.remote_max_files:
        if not m.skip:
            raise RemoteError(f"{remote.path} matches {len(todo):,} files; one run loads at most {st.remote_max_files:,} "
                              "(CONDUIT_REMOTE_MAX_FILES). Narrow the pattern, or turn on 'skip files already loaded' "
                              "to load them over several runs.")
        m.more_left = len(todo) - st.remote_max_files
        todo = todo[: st.remote_max_files]
        m.notes.append(f"{m.more_left:,} more new file(s) are left for the next run (at most {st.remote_max_files:,} per run)")
    m.chosen = todo
    return m


def _skip_notes(m: Match, remote: RemoteIn, skip_note: str | None, planning: bool) -> list[str]:
    notes = list(m.notes)
    if skip_note:
        notes.append(skip_note)
    if m.status and remote.match != "exact":
        loaded = sum(1 for v in m.status.values() if v == "loaded")
        changed = [e.path for e in m.matches if m.status.get(e.full) == "changed"]
        if planning and m.skip and loaded:
            notes.append(f"{loaded} of {len(m.matches)} matching file(s) are already loaded into this target: "
                         f"a run now loads {len(m.chosen)}")
        if changed and (not m.skip or any(e.path in changed for e in m.chosen)):
            notes.append(f"{len(changed)} file(s) changed since they were loaded (overwritten in place: "
                         f"{', '.join(changed[:3])}{' …' if len(changed) > 3 else ''}). In append mode their rows load again.")
    return notes


# ---- previews: never a big download inside a request ------------------------------------------

def _cut_lines(data: bytes) -> bytes:
    """Drop the last, probably partial, line."""
    i = data.rfind(b"\n")
    return data[: i + 1] if i >= 0 else data


def _head(c: StorageConnector, e: RemoteEntry, n: int) -> bytes:
    """The first n bytes (or the whole file when smaller)."""
    out = bytearray()
    gen = c.stream(e.path)
    try:
        for chunk in gen:
            out += chunk
            if len(out) >= n:
                break
    finally:
        gen.close()
    return bytes(out[:n])


def _gunzip_head(c: StorageConnector, e: RemoteEntry, n: int) -> bytes:
    """The first n uncompressed bytes of a gzip file, decompressing only as much as needed."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = bytearray()
    gen = c.stream(e.path)
    try:
        for chunk in gen:
            out += d.decompress(chunk, n - len(out))
            while d.unconsumed_tail and len(out) < n:
                out += d.decompress(d.unconsumed_tail, n - len(out))
            if len(out) >= n or d.eof:
                break
    except zlib.error as err:
        raise RemoteError(f"{e.name} is not a valid gzip file: {err}") from err
    finally:
        gen.close()
    return bytes(out)


def _write_cache(key: str, name: str, data: bytes) -> Path:
    dest = filecache.target(key, name)
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(data)
    part.replace(dest)
    filecache.evict(keep=dest)
    return dest


@dataclass
class Preview:
    local: Path  # what to read (a whole cached copy, or a sample of it)
    sampled: bool
    rows: int | None = None  # exact row count when known without the whole file (Parquet footer)
    notes: list[str] = field(default_factory=list)


def preview_file(c: StorageConnector, e: RemoteEntry, fmt: str | None) -> Preview:
    """A local copy (or sample) of a remote file that is cheap to inspect, from the cache when possible."""
    st = get_settings()
    inner, comp = split_compression(e.name)
    if fmt is None and comp != "zip":
        fmt = _name_format(e.name)[0]
    if fmt is None and comp != "zip":
        raise RemoteError(f"Can't tell the format of {e.name} from its name: choose the file format")
    full_max = st.remote_inspect_full_mb * 1024 * 1024
    key = f"preview:{e.full}:{e.version}"
    # Whole files: small ones, archives and Excel (neither can be sampled), and files of unknown size (no ranges)
    whole = comp == "zip" or fmt == "excel" or e.size is None or e.size <= full_max
    if whole:
        if hit := filecache.lookup(key, e.name):
            return Preview(hit, False)
        size = e.size or 0
        if size > st.remote_max_file_mb * 1024 * 1024:
            raise RemoteError(f"{e.name} is larger than the {st.remote_max_file_mb:,} MB limit")
        dest = filecache.target(key, e.name)
        filecache.ensure_space(dest.parent, size, f"previewing {e.name}")
        c.download(e.path, dest, st.remote_max_file_mb * 1024 * 1024)
        filecache.evict(keep=dest)
        return Preview(dest, False)
    big = f"{e.size / 1024 ** 3:,.1f} GB" if e.size and e.size >= 1024 ** 3 else f"{(e.size or 0) / 1024 ** 2:,.0f} MB"
    if fmt == "parquet":
        return _preview_parquet(c, e, key, big)
    sample_name = inner if comp == "gzip" else e.name
    if hit := filecache.lookup(key + ":head", sample_name):
        return Preview(hit, True, notes=[_sample_note(big)])
    data = _gunzip_head(c, e, HEAD_BYTES) if comp == "gzip" else _head(c, e, HEAD_BYTES)
    if fmt == "json" and data.lstrip()[:1] == b"[":
        raise RemoteError(f"{e.name} is a large JSON array ({big}): it can't be previewed from its first megabytes. "
                          "NDJSON (one object per line) can; a load reads the whole file either way.")
    return Preview(_write_cache(key + ":head", sample_name, _cut_lines(data)), True, notes=[_sample_note(big)])


def _sample_note(big: str) -> str:
    return f"Previewed from the first {HEAD_BYTES // 1024 // 1024} MB of a {big} file: the row count is unknown, counted during the load."


_TOTAL_ROWS = b"conduit_total_rows"  # the real file's row count, kept in the cached sample's metadata


def _preview_parquet(c: StorageConnector, e: RemoteEntry, key: str, big: str) -> Preview:
    """Schema and row count from the footer, and only the first rows: a row group bigger than BIG_ROW_GROUP is
    read through a small buffered stream instead of being loaded whole."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    note = [f"Previewed from the first rows of a {big} Parquet file (the row count comes from its footer)."]
    if hit := filecache.lookup(key + ":head", e.name):
        total = int((pq.read_schema(hit).metadata or {}).get(_TOTAL_ROWS, b"0")) or None
        return Preview(hit, True, total, note)
    try:
        with c.open(e.path) as f:
            meta = pq.ParquetFile(f).metadata
            rg_big = meta.num_row_groups > 0 and meta.row_group(0).total_byte_size > BIG_ROW_GROUP
            f.seek(0)
            pf = pq.ParquetFile(f, pre_buffer=False, buffer_size=1024 * 1024) if rg_big else pq.ParquetFile(f)
            batch = next(pf.iter_batches(batch_size=2000), None)
            table = pa.Table.from_batches([batch]) if batch is not None else pf.schema_arrow.empty_table()
    except RangeNotSupported as err:
        raise RemoteError(f"{e.name} ({big}) can't be previewed: the server does not support partial downloads, and "
                          "a Parquet preview needs the end of the file. A load reads the whole file either way.") from err
    table = table.replace_schema_metadata({**(table.schema.metadata or {}), _TOTAL_ROWS: str(meta.num_rows).encode()})
    dest = filecache.target(key + ":head", e.name)
    part = dest.with_name(dest.name + ".part")
    pq.write_table(table, part)
    part.replace(dest)
    filecache.evict(keep=dest)
    return Preview(dest, True, meta.num_rows, note)


def label_of(c: StorageConnector, path: str) -> str:
    """How a file is named in splits, rejects and logs: its path under the base, or for a URL the URL without
    its query string (pre-signed links carry tokens there)."""
    return c.display_path(path) if isinstance(c, HTTPConnector) else path


def _file_adapter(local: Path, remote: RemoteIn, label: str, unpack_dir: Path | None = None) -> FileAdapter:
    try:
        return FileAdapter(FileSpec(path=str(local), **remote.parse_options()), trusted=local, label=label, unpack_dir=unpack_dir)
    except UnsupportedFormat as err:
        raise RemoteError(str(err)) from err


def _resolve(session: Session, remote: RemoteIn, tkey: str | None, target_mode: str | None):
    """Match `remote` now. `chosen` is what a run would load (skip applied); previews read from `matches`."""
    c, conn = storage(session, remote.connection_id)
    skip, skip_note = effective_skip(remote, target_mode)
    loaded = loaded_versions(session, source_key(remote), tkey) if tkey else None
    try:
        m = resolve_match(c, remote, loaded, skip)
    except RemoteError as err:
        raise RemoteError(mask_text(str(err), remote.path)) from err
    remember(session, conn, c)
    return c, conn, m, skip_note


def inspect(session: Session, remote: RemoteIn, sample_rows: int = 20, tkey: str | None = None,
            target_mode: str | None = None) -> tuple[FileInspection, dict[str, Any], dict[str, Any]]:
    """(inspection of the first matching file, the match summary, which file was inspected)."""
    c, conn, m, skip_note = _resolve(session, remote, tkey, target_mode)
    if not m.matches:
        raise RemoteError(f"No files match {remote.path}")
    e = m.matches[0]  # all: the first by name (its columns are the load's); newest: the newest
    p = preview_file(c, e, remote.format)
    remember(session, conn, c)
    try:
        res = inspect_local(p.local, sample_rows, fmt=remote.format)
    except UnsupportedFormat as err:
        raise RemoteError(str(err)) from err
    res.path, res.size_bytes = c.display_path(e.path), e.size
    res.compression = split_compression(e.name)[1]
    if p.sampled:
        res.row_estimate, res.exact = p.rows, p.rows is not None
        res.notes = [n for n in res.notes if "not pre-counted" not in n] + p.notes
    res.notes += _skip_notes(m, remote, skip_note, planning=True)
    return res, m.summary(c), {"name": e.name, "path": e.path, "display": c.display_path(e.path), "size": e.size,
                               "modified": e.modified, "sampled": p.sampled}


def match_summary(session: Session, remote: RemoteIn, tkey: str | None, target_mode: str | None) -> dict[str, Any]:
    c, _, m, skip_note = _resolve(session, remote, tkey, target_mode)
    out = m.summary(c)
    out["notes"] = _skip_notes(m, remote, skip_note, planning=True)
    return out


def preview_source(session: Session, remote: RemoteIn, tkey: str | None = None,
                   target_mode: str | None = None) -> MultiFileAdapter:
    """A source for planning / previewing: every matching file as a split, only the first one fetched (sampled)."""
    c, conn, m, skip_note = _resolve(session, remote, tkey, target_mode)
    if not m.matches:
        raise RemoteError(f"No files match {remote.path}")
    files = m.matches if remote.match == "all" else m.matches[:1]
    p = preview_file(c, files[0], remote.format)
    remember(session, conn, c)
    first = SourceFile(label_of(c, files[0].path), _file_adapter(p.local, remote, label_of(c, files[0].path)), files[0].size)
    notes = p.notes + _skip_notes(m, remote, skip_note, planning=True)
    return MultiFileAdapter([first] + [SourceFile(label_of(c, e.path), None, e.size) for e in files[1:]], notes)


# ---- loads: download first, then read locally ----------------------------------------------------

def job_dir(job_id: uuid.UUID | str) -> Path:
    return Path(get_settings().remote_dir) / "jobs" / str(job_id)


def remove_job_dir(job_id: uuid.UUID | str) -> None:
    shutil.rmtree(job_dir(job_id), ignore_errors=True)


def _download_all(c: StorageConnector, entries: list[RemoteEntry], dest: Path, what: str,
                  job_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    st = get_settings()
    cap = st.remote_max_file_mb * 1024 * 1024
    too_big = [e for e in entries if (e.size or 0) > cap]
    if too_big:
        raise RemoteError(f"{too_big[0].path} is {too_big[0].size / 1024 ** 2:,.0f} MB: more than the "
                          f"{st.remote_max_file_mb:,} MB limit per file (CONDUIT_REMOTE_MAX_FILE_MB)")
    files_dir = dest / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    filecache.ensure_space(files_dir, sum(e.size or 0 for e in entries), what)
    out = []
    for i, e in enumerate(entries):
        local = files_dir / f"{i:04d}_{filecache.safe_name(e.name)}"
        n = c.download(e.path, local, cap)
        if job_id is not None:
            joblog.push(job_id, "info", f"downloaded {label_of(c, e.path)} ({n / 1024 ** 2:,.1f} MB)")
        out.append(_record(i, e, local.name, n, label_of(c, e.path)))
    return out


def _record(i: int, e: RemoteEntry, local: str, size: int | None, label: str) -> dict[str, Any]:
    return {"idx": i, "path": e.path, "label": label, "name": e.name, "full": e.full, "version": e.version,
            "size": size if size is not None else e.size, "modified": e.modified.isoformat() if e.modified else None,
            "local": local}


@dataclass
class RemoteCtx:
    """What the runner needs to record finished files (RemoteLoad) after their splits."""

    skey: str
    tkey: str
    records: list[dict[str, Any]]
    record: bool  # newest / all: remember loaded files (also with skip off, so turning it on later works)


def job_source(session: Session, job_id: uuid.UUID, remote: RemoteIn, tkey: str,
               target_mode: str) -> tuple[MultiFileAdapter, RemoteCtx]:
    """The source of a load job: the chosen files, downloaded once. A retry reuses manifest.json and the files."""
    d = job_dir(job_id)
    manifest = d / "manifest.json"
    c, conn = storage(session, remote.connection_id)
    notes: list[str] = []
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        records = data["files"]
        for rec in records:  # a file that vanished from the folder: fetch the same version again, or stop
            local = d / "files" / rec["local"]
            if not local.exists():
                e = c.info(rec["path"])
                if e.version != rec["version"]:
                    raise RemoteError(f"{rec['label']} changed on the server since this job started, and its downloaded "
                                      "copy is gone: start a new load instead of retrying")
                c.download(rec["path"], local, get_settings().remote_max_file_mb * 1024 * 1024)
        notes = data.get("notes", [])
    else:
        skip, skip_note = effective_skip(remote, target_mode)
        loaded = loaded_versions(session, source_key(remote), tkey) if skip else None
        m = resolve_match(c, remote, loaded, skip)
        notes = _skip_notes(m, remote, skip_note, planning=False)
        if not m.chosen:
            raise NothingToLoad(" ".join(m.notes) or f"No files match {remote.path}")
        what = f"downloading {len(m.chosen)} file(s) from {c.display_path(remote.path) if not has_glob(remote.path) else remote.path}"
        records = _download_all(c, m.chosen, d, what, job_id)
        shown = ", ".join(r["label"] for r in records[:5]) + (f" (+{len(records) - 5} more)" if len(records) > 5 else "")
        notes.insert(0, f"Files: {mask_text(shown, remote.path)}")
        d.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"connection_id": str(remote.connection_id) if remote.connection_id else None,
                                        "pattern": mask_url(remote.path), "match": remote.match, "files": records,
                                        "notes": notes, "resolved_at": utcnow().isoformat()}, indent=1), encoding="utf-8")
    remember(session, conn, c)
    files = [SourceFile(rec["label"], _file_adapter(d / "files" / rec["local"], remote, rec["label"], d / "unpacked"),
                        rec.get("size"), rec) for rec in records]
    src = MultiFileAdapter(files, notes)
    src.check_columns()
    return src, RemoteCtx(source_key(remote), tkey, records, record=remote.match != "exact")


def compare_source(session: Session, remote: RemoteIn, dest: Path) -> MultiFileAdapter:
    """Compare reads a remote side in full: every chosen file downloaded into the comparison's work folder."""
    c, conn = storage(session, remote.connection_id)
    m = resolve_match(c, remote, None, False)
    if not m.chosen:
        raise RemoteError(f"No files match {remote.path}")
    records = _download_all(c, m.chosen, dest, f"comparing {len(m.chosen)} remote file(s)")
    remember(session, conn, c)
    src = MultiFileAdapter([SourceFile(r["label"], _file_adapter(dest / "files" / r["local"], remote, r["label"], dest / "unpacked"),
                                       r["size"], r) for r in records])
    src.check_columns()
    return src


# ---- one load at a time per source + target, and the scheduler's check ------------------------------

def _keys_of(spec: dict[str, Any]) -> tuple[str, str] | None:
    """(source_key, target_key) of a job spec whose remote source skips loaded files, else None."""
    r = (spec.get("source") or {}).get("remote")
    if not r:
        return None
    try:
        remote = RemoteIn(**r)
    except Exception:  # noqa: BLE001
        return None
    target = spec.get("target") or {}
    if not effective_skip(remote, target.get("mode"))[0]:
        return None
    return source_key(remote), target_key(target)


def _fresh(job: Job) -> bool:
    hb = job.heartbeat_at
    if hb is not None and hb.tzinfo is None:
        hb = hb.replace(tzinfo=utcnow().tzinfo)
    return hb is not None and utcnow() - hb < timedelta(seconds=90)


def overlapping_job(session: Session, spec: dict[str, Any], me: Job | None = None) -> Job | None:
    """Another queued / running job loading from the same remote source into the same target with skip-loaded on.

    Two such jobs would both pick up the same new files and load them twice. A running job always wins; between
    two that have not started writing, the older one does.
    """
    keys = _keys_of(spec)
    if keys is None:
        return None
    q = select(Job).where(Job.status.in_(ACTIVE))
    if me is not None:
        q = q.where(Job.id != me.id)
    for other in session.scalars(q):
        if _keys_of(other.spec or {}) != keys:
            continue
        if other.status == "running":
            if _fresh(other):
                return other
            continue  # a stalled job: its worker died
        if me is None or (other.created_at, str(other.id)) < (me.created_at, str(me.id)):
            return other
    return None


def has_new_files(session: Session, spec: dict[str, Any]) -> tuple[bool, str]:
    """For the scheduler: would a run of this pipeline load anything? (yes/no, a note for the pipeline)."""
    r = (spec.get("source") or {}).get("remote")
    if not r:
        return True, ""
    remote = RemoteIn(**r)
    target = spec.get("target") or {}
    skip, _ = effective_skip(remote, target.get("mode"))
    if not skip:
        return True, ""
    if other := overlapping_job(session, spec):
        return False, f"skipped: an earlier load from this source ({other.name}) is still {other.status}"
    c, conn = storage(session, remote.connection_id)
    m = resolve_match(c, remote, loaded_versions(session, source_key(remote), target_key(target)), True)
    remember(session, conn, c)
    if not m.chosen:
        return False, " ".join(m.notes) or "no new files"
    changed = sum(1 for e in m.chosen if m.status.get(e.full) == "changed")
    return True, f"{len(m.chosen)} file(s) to load" + (f" ({changed} changed)" if changed else "")


def display(remote: RemoteIn | dict, session: Session | None = None) -> str:
    """How a remote source is named (job names, logs): s3://bucket/prefix/pattern, never a query string."""
    r = remote if isinstance(remote, RemoteIn) else RemoteIn(**remote)
    if r.connection_id is None or session is None:
        return mask_url(r.path)
    conn = session.get(Connection, r.connection_id)
    host = (conn.display_host if conn else "").rstrip("/")
    return mask_url(f"{host}/{r.path.lstrip('/')}") if host else mask_url(r.path)


def name_of(path: str) -> str:
    return posixpath.basename(mask_url(path).rstrip("/")) or path
