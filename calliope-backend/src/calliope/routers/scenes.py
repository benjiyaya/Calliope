from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException

from calliope.agent.coverage_agent import expand_scene_coverage
from calliope.agent.script_agent import generate_script
from calliope.config import settings
from calliope.db import ensure_default_clip, get_db, row_to_dict
from calliope.models.schemas import (
    ClipCreate,
    ClipReorder,
    ClipUpdate,
    ExpandClipsRequest,
    GenerateScenesRequest,
    SceneCreate,
    SceneReorder,
    SceneUpdate,
)

router = APIRouter()


def _clip_public(clip_row) -> dict[str, Any]:
    clip = row_to_dict(clip_row)
    # Legacy column: continue-from-previous was removed — never surface it.
    clip.pop("chain_from_prev", None)
    raw_settings = clip.pop("video_settings_json", None)
    if raw_settings:
        try:
            clip["video_settings"] = json.loads(raw_settings)
        except (json.JSONDecodeError, TypeError):
            clip["video_settings"] = None
    else:
        clip["video_settings"] = None
    raw_covered = clip.pop("dialog_lines_covered", None)
    if raw_covered:
        try:
            clip["dialog_lines_covered"] = json.loads(raw_covered)
        except (json.JSONDecodeError, TypeError):
            clip["dialog_lines_covered"] = None
    else:
        clip["dialog_lines_covered"] = None
    return clip


def _clips_for_scene(conn, scene_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM clips WHERE scene_id = ? ORDER BY order_index, id",
        (scene_id,),
    ).fetchall()
    return [_clip_public(r) for r in rows]


def _scene_with_chars(conn, scene_row, *, with_clips: bool = True) -> dict[str, Any]:
    scene = row_to_dict(scene_row)
    # Legacy column: continue-from-previous was removed — never surface it.
    scene.pop("chain_from_prev", None)
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
    scene["clips"] = _clips_for_scene(conn, scene["id"]) if with_clips else []
    return scene


def _ensure_default_clip(conn, scene_id: int, project_id: int) -> None:
    """Thin alias — the shared invariant lives in db.ensure_default_clip."""
    ensure_default_clip(conn, scene_id, project_id)


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
        clip_durations = conn.execute(
            "SELECT scene_id, SUM(COALESCE(duration_sec, 0)) AS total FROM clips "
            "WHERE project_id = ? GROUP BY scene_id",
            (project_id,),
        ).fetchall()
        per_scene = {r["scene_id"]: r["total"] or 0 for r in clip_durations}
        for s in scenes:
            for c in s["clips"]:
                c.setdefault("duration_sec", None)
        # Estimated runtime = sum over CLIPS (the renderables), not scenes.
        total = sum(
            (c.get("duration_sec") or 0) for s in scenes for c in s["clips"]
        )
        scene_budget_total = sum(per_scene.get(s["id"], 0) for s in scenes)
        return {
            "scenes": scenes,
            "estimated_duration_sec": total or scene_budget_total,
        }
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


@router.post("/{project_id}/scenes/{scene_id}/expand-clips")
async def expand_clips_endpoint(
    project_id: int,
    scene_id: int,
    payload: ExpandClipsRequest | None = None,
) -> dict[str, Any]:
    body = payload or ExpandClipsRequest()
    try:
        return await expand_scene_coverage(
            project_id,
            [scene_id],
            guidance=body.guidance,
            clip_cap=body.clip_cap,
        )
    except ValueError as exc:
        detail = str(exc)
        code = 404 if "not found" in detail.lower() or "No scenes" in detail else 422
        raise HTTPException(status_code=code, detail=detail) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/{project_id}/expand-clips")
async def expand_all_clips_endpoint(
    project_id: int,
    payload: ExpandClipsRequest | None = None,
) -> dict[str, Any]:
    body = payload or ExpandClipsRequest()
    try:
        return await expand_scene_coverage(
            project_id,
            body.scene_ids,
            guidance=body.guidance,
            clip_cap=body.clip_cap,
        )
    except ValueError as exc:
        detail = str(exc)
        code = 404 if "not found" in detail.lower() or "No scenes" in detail else 422
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
        _ensure_default_clip(conn, scene_id, project_id)
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
        _ensure_default_clip(conn, scene_id, project_id)
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


# ─────────────────────────────────────────────────────────────────────────
# Clips: the shot-level layer under each scene
# ─────────────────────────────────────────────────────────────────────────


@router.post("/{project_id}/scenes/{scene_id}/clips")
async def create_clip(project_id: int, scene_id: int, payload: ClipCreate) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        scene = conn.execute(
            "SELECT id FROM scenes WHERE id = ? AND project_id = ?",
            (scene_id, project_id),
        ).fetchone()
        if not scene:
            raise HTTPException(status_code=404, detail="Scene not found")
        covered = json.dumps(payload.dialog_lines_covered) if payload.dialog_lines_covered else None
        next_index = conn.execute(
            "SELECT COALESCE(MAX(order_index), 0) + 1 AS n FROM clips WHERE scene_id = ?",
            (scene_id,),
        ).fetchone()["n"]
        cur = conn.execute(
            """
            INSERT INTO clips (scene_id, project_id, order_index, description, shot_size,
                               dialog_lines_covered, duration_sec, workflow_id,
                               video_settings_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scene_id,
                project_id,
                payload.order_index or next_index,
                payload.description,
                payload.shot_size,
                covered,
                payload.duration_sec,
                payload.workflow_id,
                json.dumps(payload.video_settings) if payload.video_settings else None,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM clips WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _clip_public(row)
    finally:
        conn.close()


@router.patch("/{project_id}/clips/{clip_id}")
async def update_clip(project_id: int, clip_id: int, payload: ClipUpdate) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        existing = conn.execute(
            "SELECT id FROM clips WHERE id = ? AND project_id = ?",
            (clip_id, project_id),
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Clip not found")
        data = payload.model_dump(exclude_unset=True)
        video_settings = data.pop("video_settings", None)
        covered = data.pop("dialog_lines_covered", None)
        data = {k: v for k, v in data.items() if v is not None}
        if covered is not None:
            data["dialog_lines_covered"] = json.dumps(covered) if covered else None
        if video_settings is not None:
            data["video_settings_json"] = json.dumps(video_settings)
        fields = [f"{k} = :{k}" for k in data]
        params: dict[str, Any] = {"id": clip_id, **data}
        if fields:
            conn.execute(
                f"UPDATE clips SET {', '.join(fields)} WHERE id = :id", params
            )
        conn.commit()
        row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        return _clip_public(row)
    finally:
        conn.close()


@router.delete("/{project_id}/clips/{clip_id}")
async def delete_clip(project_id: int, clip_id: int) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT scene_id FROM clips WHERE id = ? AND project_id = ?",
            (clip_id, project_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Clip not found")
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM clips WHERE scene_id = ?",
            (row["scene_id"],),
        ).fetchone()["n"]
        if count <= 1:
            raise HTTPException(
                status_code=422,
                detail="A scene needs at least one clip — delete the scene instead",
            )
        conn.execute(
            "DELETE FROM clips WHERE id = ? AND project_id = ?",
            (clip_id, project_id),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@router.post("/{project_id}/scenes/{scene_id}/clips/reorder")
async def reorder_clips(project_id: int, scene_id: int, payload: ClipReorder) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        scene = conn.execute(
            "SELECT id FROM scenes WHERE id = ? AND project_id = ?",
            (scene_id, project_id),
        ).fetchone()
        if not scene:
            raise HTTPException(status_code=404, detail="Scene not found")
        for index, clip_id in enumerate(payload.clip_ids, start=1):
            conn.execute(
                "UPDATE clips SET order_index = ? WHERE id = ? AND scene_id = ?",
                (index, clip_id, scene_id),
            )
        conn.commit()
        return {"clips": _clips_for_scene(conn, scene_id)}
    finally:
        conn.close()
