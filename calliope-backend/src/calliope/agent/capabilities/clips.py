"""Clip rows and the break-into-shots pass."""

from __future__ import annotations

import json
from typing import Any

from calliope.agent.capabilities.sql import as_int, connect, touch
from calliope.db import row_to_dict

_SHOTS = {"wide", "medium", "closeUp", "insert", "overShoulder"}


def _label(scene_order: int, clip_order: int) -> str:
    return f"#{scene_order}.{clip_order}"


def list_clips(project_id: int, args: dict[str, Any] | None = None) -> dict[str, Any]:
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT c.id, c.scene_id, c.order_index, c.description, c.shot_size,
                   c.dialog_lines_covered, c.duration_sec, s.order_index AS scene_order,
                   s.heading
            FROM clips c JOIN scenes s ON s.id = c.scene_id
            WHERE c.project_id = ?
            ORDER BY s.order_index, c.order_index, c.id
            """,
            (project_id,),
        ).fetchall()
        clips = []
        for row in rows:
            item = row_to_dict(row)
            item["clip_id"] = int(item["id"])
            item["label"] = _label(int(item["scene_order"]), int(item["order_index"]))
            clips.append(item)
        scene_id = as_int((args or {}).get("scene_id"))
        if scene_id is not None:
            clips = [clip for clip in clips if int(clip["scene_id"]) == scene_id]
        return {"ok": True, "count": len(clips), "clips": clips}
    finally:
        conn.close()


def _scene_id(conn, project_id: int, args: dict[str, Any]) -> tuple[int | None, str | None]:
    scene_id = as_int(args.get("scene_id"))
    if scene_id is not None:
        row = conn.execute(
            "SELECT id FROM scenes WHERE id = ? AND project_id = ?",
            (scene_id, project_id),
        ).fetchone()
        if not row:
            return None, f"Scene {scene_id} not found in this project"
        return scene_id, None
    order = as_int(args.get("order"))
    if order is None:
        return None, "Pass scene_id or order"
    row = conn.execute(
        "SELECT id FROM scenes WHERE project_id = ? AND order_index = ?",
        (project_id, order),
    ).fetchone()
    if not row:
        return None, f"No scene at order {order}"
    return int(row["id"]), None


def add_clip(project_id: int, args: dict[str, Any]) -> dict[str, Any]:
    description = (args.get("description") or "").strip()
    if not description:
        return {"ok": False, "error": "description is required"}
    shot = args.get("shot_size") or "medium"
    if shot not in _SHOTS:
        return {"ok": False, "error": f"shot_size must be one of {sorted(_SHOTS)}"}
    conn = connect()
    try:
        scene_id, err = _scene_id(conn, project_id, args)
        if err:
            return {"ok": False, "error": err}
        nxt = conn.execute(
            "SELECT COALESCE(MAX(order_index), 0) + 1 FROM clips WHERE scene_id = ?",
            (scene_id,),
        ).fetchone()[0]
        covered = args.get("dialog_lines_covered")
        covered_json = json.dumps([int(i) for i in covered]) if covered else None
        cur = conn.execute(
            """
            INSERT INTO clips (scene_id, project_id, order_index, description,
                               shot_size, dialog_lines_covered, duration_sec)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scene_id,
                project_id,
                int(nxt),
                description,
                shot,
                covered_json,
                as_int(args.get("duration_sec")) or 8,
            ),
        )
        touch(conn, project_id)
        conn.commit()
        scene_order = conn.execute(
            "SELECT order_index FROM scenes WHERE id = ?", (scene_id,)
        ).fetchone()[0]
        return {
            "ok": True,
            "clip": {
                "id": int(cur.lastrowid),
                "label": _label(int(scene_order), int(nxt)),
                "description": description,
            },
        }
    finally:
        conn.close()


def _clip_id(conn, project_id: int, args: dict[str, Any]) -> tuple[int | None, str | None]:
    clip_id = as_int(args.get("clip_id"))
    if clip_id is not None:
        row = conn.execute(
            "SELECT id FROM clips WHERE id = ? AND project_id = ?",
            (clip_id, project_id),
        ).fetchone()
        if not row:
            return None, f"Clip {clip_id} not found in this project"
        return clip_id, None
    ref = str(args.get("ref") or "").strip().lstrip("#")
    if not ref or "." not in ref:
        return None, "Pass clip_id or ref like #3.2"
    scene_order, _, clip_order = ref.partition(".")
    scene_n, clip_n = as_int(scene_order), as_int(clip_order)
    if scene_n is None or clip_n is None:
        return None, "ref must look like #3.2"
    row = conn.execute(
        """
        SELECT c.id FROM clips c JOIN scenes s ON s.id = c.scene_id
        WHERE c.project_id = ? AND s.order_index = ? AND c.order_index = ?
        """,
        (project_id, scene_n, clip_n),
    ).fetchone()
    if not row:
        return None, f"No clip #{scene_n}.{clip_n}"
    return int(row["id"]), None


def _settings(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


def _paths(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _role_bucket(role: str) -> str | None:
    if role in ("image", "img"):
        return "image"
    if role in ("audio", "sound", "sfx"):
        return "audio"
    if role in ("video", "vid"):
        return "video"
    return None


def _map_references(
    workflow_json: str,
    images: list[str],
    audios: list[str],
    videos: list[str],
) -> dict[str, str]:
    from calliope.comfyui.parser import parse_dynamic_inputs

    try:
        nodes = json.loads(workflow_json or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(nodes, dict):
        return {}
    buckets = {"image": list(images), "audio": list(audios), "video": list(videos)}
    mapped: dict[str, str] = {}
    inputs = parse_dynamic_inputs(nodes)
    inputs.sort(key=lambda item: str(item.get("nodeId")))
    for item in inputs:
        bucket = _role_bucket(str(item.get("role") or ""))
        if bucket is None or not buckets[bucket]:
            continue
        mapped[str(item["nodeId"])] = buckets[bucket].pop(0)
    return mapped


def update_clip(project_id: int, args: dict[str, Any]) -> dict[str, Any]:
    conn = connect()
    try:
        clip_id, err = _clip_id(conn, project_id, args)
        if err:
            return {"ok": False, "error": err}
        data: dict[str, Any] = {}
        for key in ("description", "shot_size", "duration_sec"):
            if args.get(key) is not None:
                data[key] = args[key]
        if "shot_size" in data and data["shot_size"] not in _SHOTS:
            return {"ok": False, "error": f"shot_size must be one of {sorted(_SHOTS)}"}
        if args.get("dialog_lines_covered") is not None:
            data["dialog_lines_covered"] = json.dumps(
                [int(i) for i in args["dialog_lines_covered"]]
            )
        workflow_id = as_int(args.get("workflow_id"))
        if workflow_id is not None:
            data["workflow_id"] = workflow_id
        images = _paths(args.get("images"))
        audios = _paths(args.get("audio") if args.get("audio") is not None else args.get("audios"))
        videos = _paths(args.get("video") if args.get("video") is not None else args.get("videos"))
        prompt = args.get("prompt")
        wants_settings = prompt is not None or images or audios or videos or workflow_id is not None
        if wants_settings:
            row = conn.execute(
                "SELECT video_settings_json, workflow_id FROM clips WHERE id = ?",
                (clip_id,),
            ).fetchone()
            settings_json = _settings(row["video_settings_json"] if row else None)
            if isinstance(prompt, str):
                settings_json["prompt_draft"] = prompt
                settings_json["prompt_draft_meta"] = {"based_on": "agent"}
            if images or audios or videos:
                settings_json["references"] = {
                    "images": images,
                    "audios": audios,
                    "videos": videos,
                }
            chosen = workflow_id if workflow_id is not None else (
                int(row["workflow_id"]) if row and row["workflow_id"] else None
            )
            if chosen is not None and (images or audios or videos):
                workflow = conn.execute(
                    "SELECT workflow_json FROM workflows WHERE id = ?",
                    (chosen,),
                ).fetchone()
                if workflow is not None:
                    mapped = _map_references(
                        workflow["workflow_json"], images, audios, videos
                    )
                    current = settings_json.get("input_values")
                    if not isinstance(current, dict):
                        current = {}
                    current.update(mapped)
                    settings_json["input_values"] = current
            data["video_settings_json"] = json.dumps(settings_json)
        if not data:
            return {"ok": False, "error": "Nothing to update"}
        fields = ", ".join(f"{key} = :{key}" for key in data)
        conn.execute(
            f"UPDATE clips SET {fields} WHERE id = :id AND project_id = :project_id",
            {**data, "id": clip_id, "project_id": project_id},
        )
        touch(conn, project_id)
        conn.commit()
        return {"ok": True, "clip_id": clip_id}
    finally:
        conn.close()


def delete_clip(project_id: int, args: dict[str, Any]) -> dict[str, Any]:
    conn = connect()
    try:
        clip_id, err = _clip_id(conn, project_id, args)
        if err:
            return {"ok": False, "error": err}
        row = conn.execute(
            "SELECT scene_id FROM clips WHERE id = ?", (clip_id,)
        ).fetchone()
        count = conn.execute(
            "SELECT COUNT(*) FROM clips WHERE scene_id = ?", (row["scene_id"],)
        ).fetchone()[0]
        if int(count) <= 1:
            return {"ok": False, "error": "A scene needs at least one clip. delete_scene instead"}
        conn.execute("DELETE FROM clips WHERE id = ? AND project_id = ?", (clip_id, project_id))
        remaining = conn.execute(
            "SELECT id FROM clips WHERE scene_id = ? ORDER BY order_index, id",
            (row["scene_id"],),
        ).fetchall()
        for index, item in enumerate(remaining, start=1):
            conn.execute("UPDATE clips SET order_index = ? WHERE id = ?", (index, item["id"]))
        touch(conn, project_id)
        conn.commit()
        return {"ok": True, "deleted_clip_id": clip_id}
    finally:
        conn.close()


def update_scene(project_id: int, args: dict[str, Any]) -> dict[str, Any]:
    conn = connect()
    try:
        scene_id, err = _scene_id(conn, project_id, args)
        if err:
            return {"ok": False, "error": err}
        data = {
            key: args[key]
            for key in ("heading", "action", "dialog", "duration_sec", "beat_id")
            if args.get(key) is not None
        }
        if not data:
            return {"ok": False, "error": "Nothing to update"}
        fields = ", ".join(f"{key} = :{key}" for key in data)
        conn.execute(
            f"UPDATE scenes SET {fields} WHERE id = :id AND project_id = :project_id",
            {**data, "id": scene_id, "project_id": project_id},
        )
        touch(conn, project_id)
        conn.commit()
        row = conn.execute(
            "SELECT id, order_index, heading FROM scenes WHERE id = ?", (scene_id,)
        ).fetchone()
        return {"ok": True, "scene": row_to_dict(row)}
    finally:
        conn.close()


def delete_scene(project_id: int, args: dict[str, Any]) -> dict[str, Any]:
    conn = connect()
    try:
        scene_id, err = _scene_id(conn, project_id, args)
        if err:
            return {"ok": False, "error": err}
        cur = conn.execute(
            "DELETE FROM scenes WHERE id = ? AND project_id = ?",
            (scene_id, project_id),
        )
        if cur.rowcount == 0:
            return {"ok": False, "error": f"Scene {scene_id} not found"}
        remaining = conn.execute(
            "SELECT id FROM scenes WHERE project_id = ? ORDER BY order_index, id",
            (project_id,),
        ).fetchall()
        for index, item in enumerate(remaining, start=1):
            conn.execute(
                "UPDATE scenes SET order_index = ? WHERE id = ?", (index, item["id"])
            )
        touch(conn, project_id)
        conn.commit()
        return {"ok": True, "deleted_scene_id": scene_id}
    finally:
        conn.close()


def reorder_scenes(project_id: int, args: dict[str, Any]) -> dict[str, Any]:
    raw = args.get("scene_ids") or []
    if not raw:
        return {"ok": False, "error": "scene_ids is required"}
    conn = connect()
    try:
        for index, scene_id in enumerate(raw, start=1):
            sid = as_int(scene_id)
            if sid is None:
                return {"ok": False, "error": "scene_ids must be integers"}
            conn.execute(
                "UPDATE scenes SET order_index = ? WHERE id = ? AND project_id = ?",
                (index, sid, project_id),
            )
        touch(conn, project_id)
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()
