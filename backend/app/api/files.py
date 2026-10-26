import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool

from ..config import get_settings
from ..connectors.paths import PathNotAllowed, resolve_allowed
from ..deps import admin_only
from ..files import FileInspection, UnsupportedFormat, detect_format, inspect_file, split_compression
from ..schemas import FileEntryOut, FileInspectIn, FileInspectOut

router = APIRouter(prefix="/files", tags=["files"], dependencies=[Depends(admin_only)])


@router.post("/inspect", response_model=FileInspectOut)
async def inspect(body: FileInspectIn) -> FileInspectOut:
    """Inspect a file already on a mounted/server folder (must resolve inside an allowed root)."""
    try:
        opts = {"header": body.header, "skip_rows": body.skip_rows, "delimiter": body.delimiter}
        result: FileInspection = await run_in_threadpool(inspect_file, body.path, body.sample_rows, opts)
    except (PathNotAllowed, UnsupportedFormat) as e:
        raise HTTPException(422, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"{type(e).__name__}: {e}"[:400]) from e
    return FileInspectOut(**result.to_dict())


def _list_dir(path: str) -> list[FileEntryOut]:
    target = resolve_allowed(path) if path.strip() else resolve_allowed(get_settings().data_dir)
    if not target.exists() or not target.is_dir():
        raise NotADirectoryError(f"{target} is not a directory")
    out = []
    own = {Path(get_settings().remote_dir).resolve(), Path(get_settings().compare_dir).resolve()}  # Conduit's work folders
    for p in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if p.name.startswith(".") or p.resolve() in own:
            continue
        is_dir = p.is_dir()
        fmt = comp = None
        if not is_dir:
            try:
                fmt, comp = detect_format(p), split_compression(p.name)[1]
            except UnsupportedFormat:
                fmt = None
        st = p.stat()
        out.append(FileEntryOut(
            name=p.name, path=str(p), is_dir=is_dir, size_bytes=None if is_dir else st.st_size,
            modified_at=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc), format=fmt, compression=comp,
        ))
    return out


@router.get("/roots")
def roots() -> list[str]:
    """The allowed top-level folders, so a folder browser knows where it cannot go further up."""
    return [str(Path(r).resolve()) for r in get_settings().file_roots]


@router.get("/list", response_model=list[FileEntryOut])
async def list_dir(path: str = "") -> list[FileEntryOut]:
    """Browse a folder under an allowed root (defaults to CONDUIT_DATA_DIR). Directories sort first."""
    try:
        return await run_in_threadpool(_list_dir, path)
    except PathNotAllowed as e:
        raise HTTPException(422, str(e)) from e
    except NotADirectoryError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload(file: UploadFile = File(...)) -> dict:
    """Browser upload: stored under <data dir>/uploads/<id>/ (an allowed root), ready for /files/inspect and a load job."""
    from ..config import get_settings
    from ..runtime import current_settings

    limit_mb = current_settings().upload_max_mb  # never above the ceiling nginx in front was started with
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(file.filename or "upload").name).strip(". ") or "upload"
    dest_dir = Path(get_settings().data_dir) / "uploads" / uuid.uuid4().hex[:12]
    dest = dest_dir / name

    def _save() -> int:
        dest_dir.mkdir(parents=True, exist_ok=True)
        size = 0
        with dest.open("wb") as out:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > limit_mb * 1024 * 1024:
                    raise ValueError(f"File is larger than {limit_mb:,} MB")
                out.write(chunk)
        return size

    try:
        size = await run_in_threadpool(_save)
    except ValueError as e:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise HTTPException(413, str(e)) from e
    return {"path": str(dest), "name": name, "size_bytes": size}
