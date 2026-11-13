"""Review card for image and video generation.

The enqueue tools return this payload and do not queue a job. The chat card
shows the prompt and the reference media. Generation runs only after the
user presses Generate, which calls ``commit_review``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from calliope.agent.capabilities.sql import as_int, connect
from calliope.db import row_to_dict

_IMAGE = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_AUDIO = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac"}
_VIDEO = {".mp4", ".webm", ".mov", ".mkv"}


def _bucket(path: str) -> str | None:
    suffix = Path(path).suffix.lower()
    if suffix in _IMAGE:
        return "images"
    if suffix in _AUDIO:
        return "audios"
    if suffix in _VIDEO:
        return "videos"
    return None


def _collect(value: Any, found: dict[str, list[str]]) -> None:
    if isinstance(value, str):
        bucket = _bucket(value.strip())
        if bucket and value not in found[bucket]:
            found[bucket].append(value)
        return
    if isinstance(value, dict):
        for item in value.values():
            _collect(item, found)
    elif isinstance(value, list):
        for item in value:
            _collect(item, found)


def _empty_media() -> dict[str, list[str]]:
    return {"images": [], "audios": [], "videos": []}


def _prompt(*parts: Any) -> str:
    for part in parts:
        text = str(part or "").strip()
        if text:
            return text[:8000]
    return ""


def asset_review(project_id: int, args: dict[str, Any]) -> dict[str, Any]:
    """The images enqueue would generate, without queueing them."""
    character_ids = args.get("character_ids")
    location_ids = args.get("location_ids")
    item_ids = args.get("item_ids")
    missing_only = bool(args.get("all_missing", True))
    conn = connect()
    try:
        characters = [
            row_to_dict(row)
            for row in conn.execute(
                "SELECT * FROM characters WHERE project_id = ? ORDER BY id",
                (project_id,),
            ).fetchall()
        ]
        locations = [
            row_to_dict(row)
            for row in conn.execute(
                "SELECT * FROM locations WHERE project_id = ? ORDER BY id",
                (project_id,),
            ).fetchall()
        ]
        items = [
            row_to_dict(row)
            for row in conn.execute(
                "SELECT * FROM items WHERE project_id = ? ORDER BY id",
                (project_id,),
            ).fetchall()
        ]
    finally:
        conn.close()

    rows: list[dict[str, Any]] = []

    def _keep(row_id: int, wanted: Any) -> bool:
        return wanted is None or row_id in wanted

    for row in characters:
        if not _keep(int(row["id"]), character_ids):
            continue
        image = row.get("sheet_path") or row.get("portrait_path")
        if missing_only and image:
            continue
        media = _empty_media()
        if image:
            media["images"].append(image)
        rows.append(
            {
                "id": int(row["id"]),
                "entity": "character",
                "name": row.get("name") or "Character",
                "prompt": _prompt(
                    row.get("consistency_prompt"), row.get("appearance"), row.get("name")
                ),
                **media,
            }
        )
    include_locations = location_ids is not None or character_ids is None
    if include_locations:
        for row in locations:
            if not _keep(int(row["id"]), location_ids):
                continue
            if character_ids is not None and location_ids is None:
                continue
            image = row.get("reference_image_path")
            if missing_only and image:
                continue
            media = _empty_media()
            if image:
                media["images"].append(image)
            rows.append(
                {
                    "id": int(row["id"]),
                    "entity": "location",
                    "name": row.get("name") or "Location",
                    "prompt": _prompt(
                        row.get("consistency_prompt"), row.get("description"), row.get("name")
                    ),
                    **media,
                }
            )
    include_items = item_ids is not None or (character_ids is None and location_ids is None)
    if include_items:
        for row in items:
            if not _keep(int(row["id"]), item_ids):
                continue
            if (character_ids is not None or location_ids is not None) and item_ids is None:
                continue
            image = row.get("reference_image_path")
            if missing_only and image:
                continue
            media = _empty_media()
            if image:
                media["images"].append(image)
            rows.append(
                {
                    "id": int(row["id"]),
                    "entity": "item",
                    "name": row.get("name") or "Item",
                    "prompt": _prompt(
                        row.get("consistency_prompt"), row.get("description"), row.get("name")
                    ),
                    **media,
                }
            )
    if not rows:
        return {
            "ok": False,
            "error": "Nothing to generate. Add a character, location, or item first.",
        }
    return {
        "ok": True,
        "awaiting_review": True,
        "scope": "render",
        "question": "Review the image prompt and references, then generate.",
        "options": ["Generate", "Revise"],
        "review": {"kind": "image", "items": rows},
    }


def video_review(project_id: int, args: dict[str, Any]) -> dict[str, Any]:
    """The clips enqueue would render, without queueing them."""
    wanted = args.get("clip_ids")
    conn = connect()
    try:
        clips = [
            row_to_dict(row)
            for row in conn.execute(
                """
                SELECT c.*, s.heading, s.action, s.dialog, s.order_index AS scene_order,
                       s.env_image_path, s.location_id
                FROM clips c JOIN scenes s ON s.id = c.scene_id
                WHERE c.project_id = ?
                ORDER BY s.order_index, c.order_index
                """,
                (project_id,),
            ).fetchall()
        ]
    finally:
        conn.close()
    rows: list[dict[str, Any]] = []
    for clip in clips:
        clip_id = int(clip["id"])
        if isinstance(wanted, list) and wanted and clip_id not in [as_int(item) for item in wanted]:
            continue
        media = _empty_media()
        if clip.get("env_image_path"):
            media["images"].append(clip["env_image_path"])
        settings_raw = clip.get("video_settings_json")
        if settings_raw:
            try:
                _collect(json.loads(settings_raw), media)
            except json.JSONDecodeError:
                pass
        draft = ""
        if settings_raw:
            try:
                draft = str(json.loads(settings_raw).get("prompt_draft") or "")
            except json.JSONDecodeError:
                draft = ""
        label = (
            f"#{clip.get('scene_order')}.{clip.get('order_index')} {clip.get('heading') or ''}"
        ).strip()
        rows.append(
            {
                "id": clip_id,
                "entity": "clip",
                "name": label,
                "prompt": _prompt(draft, clip.get("description"), clip.get("action")),
                **media,
            }
        )
    if not rows:
        return {"ok": False, "error": "No clips to render."}
    return {
        "ok": True,
        "awaiting_review": True,
        "scope": "render",
        "question": "Review the video prompt and references, then generate.",
        "options": ["Generate", "Revise"],
        "review": {"kind": "video", "items": rows},
    }


def _save_prompt(conn: Any, table: str, entity_id: int, project_id: int, prompt: str) -> None:
    conn.execute(
        f"UPDATE {table} SET consistency_prompt = ? WHERE id = ? AND project_id = ?",
        (prompt, entity_id, project_id),
    )


def _save_clip_prompt(conn: Any, clip_id: int, project_id: int, prompt: str) -> None:
    row = conn.execute(
        "SELECT video_settings_json FROM clips WHERE id = ? AND project_id = ?",
        (clip_id, project_id),
    ).fetchone()
    if row is None:
        return
    try:
        data = json.loads(row["video_settings_json"] or "{}")
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["prompt_draft"] = prompt
    data["prompt_draft_meta"] = {"based_on": "review"}
    conn.execute(
        "UPDATE clips SET video_settings_json = ? WHERE id = ? AND project_id = ?",
        (json.dumps(data), clip_id, project_id),
    )


async def _commit_canvas(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Queue a reviewed canvas card the same way the Regenerate dialog does."""
    from calliope.comfyui.parser import parse_dynamic_inputs
    from calliope.queue.manager import queue_manager
    from calliope.routers.playground import ensure_playground_project

    jobs = []
    conn = connect()
    try:
        for item in items:
            if item.get("entity") != "canvas_node":
                continue
            node_id = as_int(item.get("id"))
            prompt = str(item.get("prompt") or "").strip()
            if node_id is None or not prompt:
                continue
            node = conn.execute(
                """
                SELECT n.*, c.project_id AS canvas_project_id, c.id AS canvas_id
                FROM canvas_node n JOIN canvas c ON c.id = n.canvas_id
                WHERE n.id = ? AND n.deleted = 0
                """,
                (node_id,),
            ).fetchone()
            if node is None or node["workflow_id"] is None:
                continue
            workflow = conn.execute(
                "SELECT id, kind, is_enabled, workflow_json FROM workflows WHERE id = ?",
                (node["workflow_id"],),
            ).fetchone()
            if workflow is None or not workflow["is_enabled"]:
                continue
            try:
                nodes = json.loads(workflow["workflow_json"] or "{}")
            except json.JSONDecodeError:
                continue
            prompt_node = None
            for parsed in parse_dynamic_inputs(nodes if isinstance(nodes, dict) else {}):
                if parsed.get("role") == "prompt":
                    prompt_node = str(parsed["nodeId"])
                    break
            if prompt_node is None:
                continue
            try:
                values = json.loads(node["input_values_json"] or "{}")
            except json.JSONDecodeError:
                values = {}
            if not isinstance(values, dict):
                values = {}
            values = dict(values)
            values[prompt_node] = prompt
            project_id = node["canvas_project_id"]
            if project_id is None:
                project_id = ensure_playground_project(conn)
            job = queue_manager.enqueue(
                project_id=int(project_id),
                kind=workflow["kind"],
                workflow_id=int(workflow["id"]),
                payload={
                    "input_values": values,
                    "prompt": prompt,
                    "canvas_id": int(node["canvas_id"]),
                    "source": "canvas",
                },
            )
            jobs.append(job)
        conn.commit()
    finally:
        conn.close()
    if not jobs:
        return {"ok": False, "error": "Nothing to generate."}
    return {"ok": True, "jobs": jobs}


async def commit_review(
    project_id: int | None,
    session_id: int | None,
    kind: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Write the reviewed prompts, then queue only those rows."""
    if kind == "canvas":
        return await _commit_canvas(items)
    tables = {"character": "characters", "location": "locations", "item": "items"}
    ids: dict[str, list[int]] = {"character": [], "location": [], "item": []}
    prompts: dict[int, str] = {}
    conn = connect()
    try:
        if kind == "image":
            for item in items:
                entity = str(item.get("entity") or "")
                entity_id = as_int(item.get("id"))
                prompt = str(item.get("prompt") or "").strip()
                if entity not in tables or entity_id is None or not prompt:
                    continue
                _save_prompt(conn, tables[entity], entity_id, project_id, prompt)
                ids[entity].append(entity_id)
            conn.commit()
        else:
            for item in items:
                if item.get("entity") != "clip":
                    continue
                clip_id = as_int(item.get("id"))
                prompt = str(item.get("prompt") or "").strip()
                if clip_id is None or not prompt:
                    continue
                _save_clip_prompt(conn, clip_id, project_id, prompt)
                prompts[clip_id] = prompt
            conn.commit()
    finally:
        conn.close()

    if kind == "image":
        from calliope.agent.asset_agent import enqueue_asset_jobs

        if not any(ids.values()):
            return {"ok": False, "error": "Nothing to generate."}
        jobs = await enqueue_asset_jobs(
            project_id,
            character_ids=ids["character"],
            location_ids=ids["location"],
            item_ids=ids["item"],
            missing_only=False,
            session_id=session_id,
        )
        return {"ok": True, "jobs": jobs}
    from calliope.agent.video_agent import enqueue_video_jobs

    if not prompts:
        return {"ok": False, "error": "No clips to render."}
    jobs = await enqueue_video_jobs(
        project_id,
        clip_ids=list(prompts),
        prompts=prompts,
        session_id=session_id,
    )
    return {"ok": True, "jobs": jobs}
