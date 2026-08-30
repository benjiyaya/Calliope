"""Standalone ComfyUI playground — generate freely, attach favorites to projects."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from calliope import config
from calliope.agent.prompts import item_reference_prompt
from calliope.db import get_db, row_to_dict
from calliope.events.bus import event_bus
from calliope.queue.manager import queue_manager

router = APIRouter()

PLAYGROUND_STATUS = "system"
PLAYGROUND_TITLE = "Playground Scratch"


class PlaygroundGenerate(BaseModel):
    workflow_id: int
    input_values: dict[str, Any] = Field(default_factory=dict)


class PlaygroundAttach(BaseModel):
    path: str
    project_id: int
    target: Literal["character_sheet", "location", "item", "scene"]
    character_id: int | None = None
    location_id: int | None = None
    item_id: int | None = None  # ignored: items always insert a new row
    scene_id: int | None = None
    name: str | None = None


def _job_public(job: dict[str, Any]) -> dict[str, Any]:
    out = dict(job)
    out["payload"] = json.loads(job.get("payload_json") or "{}")
    out["output_paths"] = json.loads(job.get("output_paths_json") or "[]")
    out.pop("payload_json", None)
    out.pop("output_paths_json", None)
    return out


def ensure_playground_project(conn) -> int:
    row = conn.execute(
        "SELECT id FROM projects WHERE status = ? ORDER BY id ASC LIMIT 1",
        (PLAYGROUND_STATUS,),
    ).fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute(
        """
        INSERT INTO projects (title, idea, status)
        VALUES (?, ?, ?)
        """,
        (
            PLAYGROUND_TITLE,
            "System scratch for ComfyUI playground jobs — hidden from project list",
            PLAYGROUND_STATUS,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _assert_path_in_assets(path: str) -> Path:
    assets_root = config.settings.assets_dir.resolve()
    target = Path(path).resolve()
    try:
        target.relative_to(assets_root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Path outside assets directory") from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return target


def _safe_unlink_asset(path_str: str) -> bool:
    """Delete a file if it resolves under assets_dir. Missing files count as already gone."""
    assets_root = config.settings.assets_dir.resolve()
    try:
        target = Path(path_str).resolve()
        target.relative_to(assets_root)
    except (ValueError, OSError):
        return False
    if not target.is_file():
        return False
    try:
        target.unlink()
        return True
    except OSError:
        return False


UPLOAD_KIND_BY_EXT: dict[str, str] = {
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
    ".mp3": "audio",
    ".wav": "audio",
    ".flac": "audio",
    ".ogg": "audio",
    # Note: core ComfyUI's LoadAudio file list is typically wav/mp3/ogg/flac/aiff.
    # .m4a uploads succeed here and land in Comfy's input dir, but some builds
    # reject it at /prompt validation — prefer wav/mp3 for ref audio.
    ".m4a": "audio",
}

UPLOAD_CHUNK_SIZE = 1024 * 1024  # 1MB
UPLOAD_MAX_BYTES = 500 * 1024 * 1024  # 500MB
_UPLOAD_PREFIX_RE = re.compile(r"^[0-9a-f]{8}-")


def _kind_for_ext(ext: str) -> str | None:
    return UPLOAD_KIND_BY_EXT.get(ext.lower())


def _uploads_dir() -> Path:
    return config.settings.assets_dir / "uploads"


def _safe_upload_name(filename: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", filename)


def _upload_display_name(filename: str) -> str:
    """Strip the uuid prefix written at upload time (``deadbeef-name.png`` → ``name.png``)."""
    if _UPLOAD_PREFIX_RE.match(filename):
        return filename[9:]
    return filename


def _clear_entity_refs(conn, paths: list[str]) -> None:
    """If playground outputs were attached to entities, clear those path columns."""
    for path in paths:
        conn.execute("UPDATE characters SET portrait_path = NULL WHERE portrait_path = ?", (path,))
        conn.execute("UPDATE characters SET sheet_path = NULL WHERE sheet_path = ?", (path,))
        conn.execute(
            "UPDATE locations SET reference_image_path = NULL WHERE reference_image_path = ?",
            (path,),
        )
        conn.execute(
            "UPDATE items SET reference_image_path = NULL WHERE reference_image_path = ?",
            (path,),
        )
        conn.execute("UPDATE scenes SET video_path = NULL WHERE video_path = ?", (path,))


@router.get("/project")
async def playground_project() -> dict[str, Any]:
    conn = get_db(config.settings.db_path)
    try:
        pid = ensure_playground_project(conn)
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        return row_to_dict(row)
    finally:
        conn.close()


@router.get("/jobs")
async def list_playground_jobs(limit: int = 50) -> list[dict[str, Any]]:
    conn = get_db(config.settings.db_path)
    try:
        pid = ensure_playground_project(conn)
    finally:
        conn.close()
    return [_job_public(j) for j in queue_manager.list_jobs(pid, None, limit)]


@router.delete("/jobs/{job_id}")
async def delete_playground_job(job_id: int) -> dict[str, Any]:
    """Delete a playground job row and its output files under assets_dir."""
    conn = get_db(config.settings.db_path)
    try:
        pid = ensure_playground_project(conn)
    finally:
        conn.close()

    job = queue_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if int(job["project_id"]) != int(pid):
        raise HTTPException(status_code=403, detail="Not a playground job")

    paths = json.loads(job.get("output_paths_json") or "[]")
    if not isinstance(paths, list):
        paths = []

    deleted_files: list[str] = []
    missing_files: list[str] = []
    for p in paths:
        if not isinstance(p, str) or not p.strip():
            continue
        if _safe_unlink_asset(p):
            deleted_files.append(p)
        else:
            missing_files.append(p)

    conn = get_db(config.settings.db_path)
    try:
        _clear_entity_refs(conn, [p for p in paths if isinstance(p, str)])
        conn.commit()
    finally:
        conn.close()

    removed = queue_manager.delete_job(job_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Job not found")

    await event_bus.publish(
        "job.deleted",
        {
            "job_id": job_id,
            "source": "playground",
            "deleted_files": deleted_files,
            "missing_files": missing_files,
        },
    )
    return {
        "ok": True,
        "job_id": job_id,
        "deleted_files": deleted_files,
        "missing_files": missing_files,
    }


@router.post("/uploads")
async def upload_media(file: UploadFile = File(...)) -> dict[str, Any]:
    """Store a client-provided media file under assets_dir/uploads for use as a job input."""
    original = Path(file.filename or "upload").name
    ext = Path(original).suffix.lower()
    kind = _kind_for_ext(ext)
    if kind is None:
        allowed = " ".join(sorted(UPLOAD_KIND_BY_EXT))
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext or '(none)'}'. Allowed extensions: {allowed}",
        )

    uploads = _uploads_dir()
    uploads.mkdir(parents=True, exist_ok=True)
    dest = uploads / f"{uuid4().hex[:8]}-{_safe_upload_name(original)}"

    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(UPLOAD_CHUNK_SIZE):
                size += len(chunk)
                if size > UPLOAD_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="File too large (max 500MB)")
                out.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise

    return {"ok": True, "path": str(dest.resolve()), "name": original, "kind": kind}


@router.get("/uploads")
async def list_uploaded_media() -> list[dict[str, Any]]:
    """List previously uploaded media files, newest first."""
    uploads = _uploads_dir()
    if not uploads.is_dir():
        return []
    entries: list[tuple[float, dict[str, Any]]] = []
    for f in uploads.iterdir():
        if not f.is_file():
            continue
        kind = _kind_for_ext(f.suffix)
        if kind is None:
            continue
        stat = f.stat()
        entries.append(
            (
                stat.st_mtime,
                {
                    "name": _upload_display_name(f.name),
                    "path": str(f.resolve()),
                    "kind": kind,
                    "size": stat.st_size,
                    "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                },
            )
        )
    entries.sort(key=lambda e: e[0], reverse=True)
    return [item for _, item in entries]


@router.post("/generate")
async def generate(payload: PlaygroundGenerate) -> dict[str, Any]:
    conn = get_db(config.settings.db_path)
    try:
        pid = ensure_playground_project(conn)
        wf = conn.execute(
            "SELECT id, kind, is_enabled FROM workflows WHERE id = ?",
            (payload.workflow_id,),
        ).fetchone()
        if not wf:
            raise HTTPException(status_code=404, detail="Workflow not found")
        if not wf["is_enabled"]:
            raise HTTPException(status_code=400, detail="Workflow is disabled")
        kind = wf["kind"]
    finally:
        conn.close()

    job = queue_manager.enqueue(
        project_id=pid,
        kind=kind,
        workflow_id=payload.workflow_id,
        scene_id=None,
        payload={"input_values": payload.input_values, "source": "playground"},
    )
    await event_bus.publish(
        "job.created",
        {"job_id": job["id"], "kind": kind, "source": "playground", "project_id": pid},
    )
    return {"ok": True, "job": _job_public(job)}


@router.post("/attach")
async def attach_to_project(payload: PlaygroundAttach) -> dict[str, Any]:
    path = str(_assert_path_in_assets(payload.path))
    conn = get_db(config.settings.db_path)
    try:
        project = conn.execute(
            "SELECT id, status FROM projects WHERE id = ?", (payload.project_id,)
        ).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        if project["status"] == PLAYGROUND_STATUS:
            raise HTTPException(status_code=400, detail="Cannot attach to playground scratch")

        if payload.target == "character_sheet":
            if not payload.character_id:
                raise HTTPException(status_code=400, detail="character_id required")
            char = conn.execute(
                "SELECT id FROM characters WHERE id = ? AND project_id = ?",
                (payload.character_id, payload.project_id),
            ).fetchone()
            if not char:
                raise HTTPException(status_code=404, detail="Character not found")
            conn.execute(
                "UPDATE characters SET sheet_path = ? WHERE id = ?",
                (path, payload.character_id),
            )
        elif payload.target == "location":
            if not payload.location_id:
                raise HTTPException(status_code=400, detail="location_id required")
            loc = conn.execute(
                "SELECT id FROM locations WHERE id = ? AND project_id = ?",
                (payload.location_id, payload.project_id),
            ).fetchone()
            if not loc:
                raise HTTPException(status_code=404, detail="Location not found")
            conn.execute(
                "UPDATE locations SET reference_image_path = ? WHERE id = ?",
                (path, payload.location_id),
            )
        elif payload.target == "item":
            # Always insert a new misc. item — Playground attach must not
            # overwrite an existing project item (item_id is ignored).
            raw_name = (payload.name or "").strip()
            if not raw_name:
                stem = Path(path).stem
                raw_name = stem.split("-", 1)[-1] if len(stem) > 9 and stem[8:9] == "-" else stem
            name = raw_name or "Playground item"
            consistency = item_reference_prompt(
                {"name": name, "description": "Added from Playground"}
            )
            cur = conn.execute(
                """
                INSERT INTO items (
                    project_id, name, description, consistency_prompt, reference_image_path
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (payload.project_id, name, "Added from Playground", consistency, path),
            )
            created_item_id = int(cur.lastrowid)
        elif payload.target == "scene":
            if not payload.scene_id:
                raise HTTPException(status_code=400, detail="scene_id required")
            scene = conn.execute(
                "SELECT id FROM scenes WHERE id = ? AND project_id = ?",
                (payload.scene_id, payload.project_id),
            ).fetchone()
            if not scene:
                raise HTTPException(status_code=404, detail="Scene not found")
            conn.execute(
                "UPDATE scenes SET video_path = ? WHERE id = ?",
                (path, payload.scene_id),
            )
        else:
            raise HTTPException(status_code=400, detail="Invalid target")

        conn.execute(
            "UPDATE projects SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (payload.project_id,),
        )
        conn.commit()
        await event_bus.publish(
            "asset.ready",
            {
                "path": path,
                "project_id": payload.project_id,
                "target": payload.target,
                "source": "playground_attach",
            },
        )
        out: dict[str, Any] = {
            "ok": True,
            "path": path,
            "target": payload.target,
            "project_id": payload.project_id,
        }
        if payload.target == "item":
            out["item_id"] = created_item_id
        return out
    finally:
        conn.close()
