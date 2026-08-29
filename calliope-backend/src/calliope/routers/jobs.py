from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from calliope.agent.video_agent import enqueue_video_jobs
from calliope.comfyui.client import ComfyUIClient
from calliope.config import settings
from calliope.db import get_db
from calliope.events.bus import event_bus
from calliope.export.runner import kill_running
from calliope.models.schemas import GenerateVideosRequest, JobCreate
from calliope.queue.manager import queue_manager

router = APIRouter()


def _job_public(job: dict[str, Any]) -> dict[str, Any]:
    out = dict(job)
    out["payload"] = json.loads(job.get("payload_json") or "{}")
    out["output_paths"] = json.loads(job.get("output_paths_json") or "[]")
    out.pop("payload_json", None)
    out.pop("output_paths_json", None)
    return out


@router.get("")
async def list_jobs(
    project_id: int | None = None,
    status: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return [_job_public(j) for j in queue_manager.list_jobs(project_id, status, limit)]


@router.get("/queue-status")
async def queue_status() -> dict[str, Any]:
    return {"paused": queue_manager.paused}


@router.post("/pause")
async def pause_queue() -> dict[str, Any]:
    queue_manager.paused = True
    return {"ok": True, "paused": True}


@router.post("/resume")
async def resume_queue() -> dict[str, Any]:
    queue_manager.paused = False
    return {"ok": True, "paused": False}


@router.post("/projects/{project_id}/generate-videos")
async def generate_videos(
    project_id: int, payload: GenerateVideosRequest | None = None
) -> dict[str, Any]:
    body = payload or GenerateVideosRequest()
    try:
        jobs = await enqueue_video_jobs(
            project_id,
            scene_ids=body.scene_ids,
            workflow_id=body.workflow_id,
            input_values_override=body.input_values,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "jobs": [_job_public(j) for j in jobs]}


@router.post("/projects/{project_id}/export")
async def export_film(project_id: int) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM scenes WHERE project_id = ? AND video_path IS NOT NULL",
            (project_id,),
        ).fetchone()
        if not row or row["n"] == 0:
            raise HTTPException(
                status_code=400,
                detail="No scene clips to export — generate videos first",
            )
        # Supersede leftover pending exports so a re-Export always starts fresh
        conn.execute(
            """
            UPDATE jobs SET status = 'failed', error = 'superseded by new export',
            completed_at = CURRENT_TIMESTAMP
            WHERE project_id = ? AND kind = 'export' AND status = 'pending'
            """,
            (project_id,),
        )
        # Commit BEFORE enqueue: queue_manager.enqueue writes on its own connection.
        conn.commit()
    finally:
        conn.close()
    job = queue_manager.enqueue(project_id=project_id, kind="export", payload={})
    await event_bus.publish(
        "job.created",
        {
            "job_id": job["id"],
            "kind": "export",
            "message": "Export film",
            "project_id": project_id,
        },
    )
    return {"ok": True, "job": _job_public(job)}


@router.post("")
async def create_job(payload: JobCreate, project_id: int = Query(...)) -> dict[str, Any]:
    job = queue_manager.enqueue(
        project_id=project_id,
        kind=payload.kind,
        workflow_id=payload.workflow_id,
        scene_id=payload.scene_id,
        payload={
            "input_values": payload.input_values or {},
            "character_id": payload.character_id,
            "location_id": payload.location_id,
        },
    )
    return _job_public(job)


@router.get("/{job_id}")
async def get_job(job_id: int) -> dict[str, Any]:
    job = queue_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_public(job)


@router.post("/{job_id}/retry")
async def retry_job(job_id: int) -> dict[str, Any]:
    job = queue_manager.retry(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_public(job)


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: int) -> dict[str, Any]:
    job = queue_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not cancellable")
    ok = queue_manager.cancel(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not cancellable")
    if job["kind"] == "export":
        # Export runs local ffmpeg, not Comfy — kill the process instead.
        kill_running(job_id)
    else:
        client = ComfyUIClient()
        try:
            await client.interrupt()
        finally:
            await client.close()
    return {"ok": True}
