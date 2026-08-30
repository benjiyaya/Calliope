from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException

from calliope.agent.script_agent import generate_script
from calliope.config import settings
from calliope.db import get_db, row_to_dict
from calliope.models.schemas import GenerateScenesRequest, SceneCreate, SceneReorder, SceneUpdate

router = APIRouter()


def _scene_with_chars(conn, scene_row) -> dict[str, Any]:
    scene = row_to_dict(scene_row)
    chars = conn.execute(
        """
        SELECT c.id, c.name, c.role, c.portrait_path, c.sheet_path
        FROM characters c
        JOIN scene_characters sc ON sc.character_id = c.id
        WHERE sc.scene_id = ?
        """,
        (scene["id"],),
    ).fetchall()
    scene["characters"] = [row_to_dict(c) for c in chars]
    scene["character_ids"] = [c["id"] for c in scene["characters"]]
    raw_settings = scene.pop("video_settings_json", None)
    if raw_settings:
        try:
            scene["video_settings"] = json.loads(raw_settings)
        except (json.JSONDecodeError, TypeError):
            scene["video_settings"] = None
    else:
        scene["video_settings"] = None
    return scene


@router.get("/{project_id}/scenes")
async def list_scenes(project_id: int) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        if not conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Project not found")
        rows = conn.execute(
            "SELECT * FROM scenes WHERE project_id = ? ORDER BY order_index",
            (project_id,),
        ).fetchall()
        scenes = [_scene_with_chars(conn, r) for r in rows]
        total = sum((s.get("duration_sec") or 0) for s in scenes)
        return {"scenes": scenes, "estimated_duration_sec": total}
    finally:
        conn.close()


@router.post("/{project_id}/generate-script")
async def generate_script_endpoint(project_id: int, payload: GenerateScenesRequest | None = None) -> dict[str, Any]:
    replace = True if payload is None else payload.replace
    scene_count = None if payload is None else payload.scene_count
    try:
        return await generate_script(project_id, replace=replace, scene_count=scene_count)
    except ValueError as exc:
        # Distinguish not-found vs constraint failures
        detail = str(exc)
        code = 404 if "not found" in detail.lower() else 422
        raise HTTPException(status_code=code, detail=detail) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/{project_id}/scenes")
async def create_scene(project_id: int, payload: SceneCreate) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        if not conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Project not found")
        cur = conn.execute(
            """
            INSERT INTO scenes
            (project_id, beat_id, order_index, heading, action, dialog, duration_sec,
             workflow_id, env_image_path, location_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                payload.beat_id,
                payload.order_index,
                payload.heading,
                payload.action,
                payload.dialog,
                payload.duration_sec,
                payload.workflow_id,
                payload.env_image_path,
                payload.location_id,
            ),
        )
        scene_id = cur.lastrowid
        for cid in payload.character_ids:
            conn.execute(
                "INSERT OR IGNORE INTO scene_characters (scene_id, character_id) VALUES (?, ?)",
                (scene_id, cid),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM scenes WHERE id = ?", (scene_id,)).fetchone()
        return _scene_with_chars(conn, row)
    finally:
        conn.close()


@router.patch("/{project_id}/scenes/{scene_id}")
async def update_scene(project_id: int, scene_id: int, payload: SceneUpdate) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        existing = conn.execute(
            "SELECT * FROM scenes WHERE id = ? AND project_id = ?",
            (scene_id, project_id),
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Scene not found")
        data = payload.model_dump(exclude_unset=True)
        char_ids = data.pop("character_ids", None)
        # video_settings arrives as a dict — the generic UPDATE path below only
        # handles scalars, so serialize it into its JSON column (or NULL it).
        video_settings = data.pop("video_settings", None)
        data = {k: v for k, v in data.items() if v is not None}
        if data:
            fields = ", ".join(f"{k} = :{k}" for k in data)
            data["id"] = scene_id
            if video_settings is not None:
                data["video_settings_json"] = json.dumps(video_settings)
                fields += ", video_settings_json = :video_settings_json"
            conn.execute(f"UPDATE scenes SET {fields} WHERE id = :id", data)
        elif video_settings is not None:
            conn.execute(
                "UPDATE scenes SET video_settings_json = ? WHERE id = ?",
                (json.dumps(video_settings), scene_id),
            )
        if char_ids is not None:
            conn.execute("DELETE FROM scene_characters WHERE scene_id = ?", (scene_id,))
            for cid in char_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO scene_characters (scene_id, character_id) VALUES (?, ?)",
                    (scene_id, cid),
                )
        conn.commit()
        row = conn.execute("SELECT * FROM scenes WHERE id = ?", (scene_id,)).fetchone()
        return _scene_with_chars(conn, row)
    finally:
        conn.close()


@router.delete("/{project_id}/scenes/{scene_id}")
async def delete_scene(project_id: int, scene_id: int) -> dict[str, bool]:
    conn = get_db(settings.db_path)
    try:
        conn.execute("DELETE FROM scene_characters WHERE scene_id = ?", (scene_id,))
        cur = conn.execute(
            "DELETE FROM scenes WHERE id = ? AND project_id = ?",
            (scene_id, project_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Scene not found")
        return {"ok": True}
    finally:
        conn.close()


@router.post("/{project_id}/scenes/reorder")
async def reorder_scenes(project_id: int, payload: SceneReorder) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        for index, scene_id in enumerate(payload.scene_ids, start=1):
            conn.execute(
                "UPDATE scenes SET order_index = ? WHERE id = ? AND project_id = ?",
                (index, scene_id, project_id),
            )
        conn.commit()
        rows = conn.execute(
            "SELECT * FROM scenes WHERE project_id = ? ORDER BY order_index",
            (project_id,),
        ).fetchall()
        return {"scenes": [_scene_with_chars(conn, r) for r in rows]}
    finally:
        conn.close()
