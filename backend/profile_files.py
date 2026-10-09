"""List and transfer files within each browser's persistent Home directory."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from . import database as db

router = APIRouter(prefix="/api/profiles/{profile_id}/files")
MAX_UPLOAD = 512 * 1024 * 1024


def profile_home(profile: dict) -> Path:
    directory = Path(profile["user_data_dir"]) / "Home"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    (directory / "Downloads").mkdir(exist_ok=True)
    return directory


def resolve_path(profile_id: str, path: str) -> tuple[Path, Path]:
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    root = profile_home(profile).resolve()
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts or "\x00" in path:
        raise HTTPException(status_code=400, detail="Path must be inside the browser Home directory")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise HTTPException(status_code=400, detail="Symbolic links cannot be transferred")
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise HTTPException(status_code=400, detail="Path must be inside the browser Home directory")
    return root, target


@router.get("")
async def list_files(profile_id: str, path: str = ""):
    root, target = resolve_path(profile_id, path)
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="Directory not found")
    entries = []
    for entry in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if entry.is_symlink():
            continue
        if not entry.is_file() and not entry.is_dir():
            continue
        stat = entry.stat()
        entries.append({"name": entry.name, "path": entry.relative_to(root).as_posix(), "is_directory": entry.is_dir(), "size": stat.st_size if entry.is_file() else None})
    return {"path": target.relative_to(root).as_posix() if target != root else "", "entries": entries}


@router.get("/download")
async def download_file(profile_id: str, path: str):
    _, target = resolve_path(profile_id, path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(target, filename=target.name, media_type="application/octet-stream")


@router.put("/upload")
async def upload_file(profile_id: str, path: str, request: Request):
    _, target = resolve_path(profile_id, path)
    if target.exists():
        raise HTTPException(status_code=409, detail="A file with this name already exists")
    if not target.parent.is_dir() or not target.name:
        raise HTTPException(status_code=400, detail="Upload directory not found")
    size = 0
    # Stream to a temporary file and publish without replacing existing data.
    fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(status_code=413, detail="Upload exceeds the 512 MiB limit")
                output.write(chunk)
        try:
            os.link(temporary, target)
        except FileExistsError:
            raise HTTPException(status_code=409, detail="A file with this name already exists")
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"name": target.name, "size": size}
