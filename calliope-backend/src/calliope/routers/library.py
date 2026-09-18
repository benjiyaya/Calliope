"""Asset Library — unlinked Playground media (uploads + scratch outputs).

Files that belong to a project (entity references, scene/clip videos) are
NEVER listed here: the library is the delete/attach surface for scratch
media only. Project data is replace-only, never deleted from this page.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from calliope.config import settings
from calliope.db import get_db
from calliope.routers.canvas import (
    _path_is_referenced,
    _playground_file_roots,
    _project_path_referenced,
)
from calliope.routers.playground import PLAYGROUND_STATUS

router = APIRouter()

MEDIA_KIND_BY_EXT: dict[str, str] = {
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".gif": "image",
    ".bmp": "image",
    ".mp4": "video",
    ".webm": "video",
    ".mov": "video",
    ".mkv": "video",
}


def _kind_for_ext(ext: str) -> str | None:
    return MEDIA_KIND_BY_EXT.get(ext.lower())


def _scratch_project_id(conn) -> int | None:
    row = conn.execute(
        "SELECT id FROM projects WHERE status = ? ORDER BY id ASC LIMIT 1",
        (PLAYGROUND_STATUS,),
    ).fetchone()
    return int(row["id"]) if row else None


def _library_roots(conn) -> list[Path]:
    """uploads/ first, then the scratch project's output folders."""
    roots = [Path(settings.assets_dir) / "uploads"]
    pid = _scratch_project_id(conn)
    if pid is not None:
        scratch = Path(settings.assets_dir) / str(pid)
        roots.extend([scratch / "image", scratch / "video"])
    return roots


class LibraryDeleteRequest(BaseModel):
    paths: list[str]


def _iter_media_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    out: list[Path] = []
    for f in folder.iterdir():
        if f.is_file() and _kind_for_ext(f.suffix) is not None:
            out.append(f)
    return out


@router.get("/media")
async def list_library_media() -> list[dict[str, Any]]:
    """Unlinked image/video files from uploads/ and the scratch outputs.

    A file is 'linked' the moment project data points at it — linked files
    are excluded entirely (they live in Project Assets now).
    """
    conn = get_db(settings.db_path)
    try:
        items: list[tuple[float, dict[str, Any]]] = []
        for root in _library_roots(conn):
            for f in _iter_media_files(root):
                path = str(f.resolve())
                if _project_path_referenced(conn, path):
                    continue
                stat = f.stat()
                items.append(
                    (
                        stat.st_mtime,
                        {
                            "path": path,
                            "name": f.name,
                            "kind": _kind_for_ext(f.suffix),
                            "size": stat.st_size,
                            "mtime": datetime.fromtimestamp(
                                stat.st_mtime, tz=timezone.utc
                            ).isoformat(),
                        },
                    )
                )
    finally:
        conn.close()
    items.sort(key=lambda e: e[0], reverse=True)
    return [item for _, item in items]


@router.delete("/media")
async def delete_library_media(payload: LibraryDeleteRequest) -> dict[str, Any]:
    """Bulk-delete unlinked library files. Referenced/project-owned/foreign
    paths are kept and reported per-path."""
    if not payload.paths:
        raise HTTPException(status_code=400, detail="paths must not be empty")
    if len(payload.paths) > 200:
        raise HTTPException(status_code=400, detail="Too many paths (max 200 per request)")

    conn = get_db(settings.db_path)
    try:
        allowed_roots = [r.resolve() for r in _library_roots(conn)]
        results: list[dict[str, Any]] = []
        deleted: list[str] = []
        for raw in payload.paths:
            path = str(raw or "").strip()
            if not path:
                continue
            try:
                target = Path(path).resolve()
                target.relative_to(Path(settings.assets_dir).resolve())
            except (ValueError, OSError):
                results.append({"path": path, "status": "kept", "reason": "outside_assets"})
                continue
            under_root = any(
                target == root or root in target.parents for root in allowed_roots
            )
            if not under_root:
                results.append({"path": path, "status": "kept", "reason": "project_owned"})
                continue
            if _path_is_referenced(conn, path):
                results.append({"path": path, "status": "kept", "reason": "referenced"})
                continue
            try:
                target.unlink()
                deleted.append(path)
                results.append({"path": path, "status": "deleted"})
            except FileNotFoundError:
                deleted.append(path)
                results.append({"path": path, "status": "deleted"})
            except OSError:
                results.append({"path": path, "status": "kept", "reason": "unlink_failed"})
    finally:
        conn.close()
    return {"ok": True, "deleted": deleted, "results": results}
