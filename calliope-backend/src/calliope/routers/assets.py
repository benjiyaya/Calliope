from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from calliope.agent.asset_agent import enqueue_asset_jobs
from calliope.config import settings
from calliope.db import get_db, row_to_dict
from calliope.models.schemas import GenerateAssetsRequest

router = APIRouter()


@router.get("/projects/{project_id}/assets")
async def list_assets(project_id: int) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        project = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        characters = [
            row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM characters WHERE project_id = ?", (project_id,)
            ).fetchall()
        ]
        locations = [
            row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM locations WHERE project_id = ?", (project_id,)
            ).fetchall()
        ]
        return {"characters": characters, "locations": locations}
    finally:
        conn.close()


@router.post("/projects/{project_id}/generate-assets")
async def generate_assets(project_id: int, payload: GenerateAssetsRequest) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        if not conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Project not found")
    finally:
        conn.close()
    jobs = await enqueue_asset_jobs(
        project_id,
        character_ids=payload.character_ids,
        location_ids=payload.location_ids,
        missing_only=payload.missing_only,
        workflow_id=payload.workflow_id,
        input_values_override=payload.input_values,
        asset_target=payload.asset_target or "sheet",
        prompt_override=payload.prompt,
    )
    return {"ok": True, "jobs": jobs}


@router.get("/file")
async def get_asset_file(path: str = Query(...)) -> FileResponse:
    assets_root = settings.assets_dir.resolve()
    target = Path(path).resolve()
    try:
        target.relative_to(assets_root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Path outside assets directory") from exc
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(target)
