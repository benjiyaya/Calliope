"""Scene tools granted to the script subagent.

list_beats returns titles only. get_beat returns one beat in full. add_scene
is the only write. There is no workspace dump: a truncated project idea is
what made the previous loop reread until it said Done.
"""

from __future__ import annotations

import json
from typing import Any

from calliope.config import settings
from calliope.db import ensure_default_clip, get_db, row_to_dict

_ACTION_PREVIEW = 240


def _connect():
    return get_db(settings.db_path)


def list_scene_heads(project_id: int) -> list[dict[str, Any]]:
    """id, order, heading. Main compares this before and after a call."""
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, order_index, heading
            FROM scenes WHERE project_id = ?
            ORDER BY order_index, id
            """,
            (project_id,),
        ).fetchall()
        return [row_to_dict(row) for row in rows]
    finally:
        conn.close()


def project_exists(project_id: int) -> bool:
    conn = _connect()
    try:
        row = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
        return row is not None
    finally:
        conn.close()


def list_beats(project_id: int) -> dict[str, Any]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, order_index, title
            FROM story_beats WHERE project_id = ?
            ORDER BY order_index, id
            """,
            (project_id,),
        ).fetchall()
        return {"ok": True, "beats": [row_to_dict(row) for row in rows]}
    finally:
        conn.close()


def get_beat(
    project_id: int,
    *,
    order: int | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """One beat, full description. Match order first, then an exact title."""
    if order is None and not (title or "").strip():
        return {"ok": False, "error": "pass order or title"}
    conn = _connect()
    try:
        row = None
        if order is not None:
            try:
                order_index = int(order)
            except (TypeError, ValueError):
                return {"ok": False, "error": "order must be an integer"}
            row = conn.execute(
                """
                SELECT id, order_index, title, description
                FROM story_beats
                WHERE project_id = ? AND order_index = ?
                """,
                (project_id, order_index),
            ).fetchone()
        if row is None and (title or "").strip():
            wanted = title.strip().lower()
            rows = conn.execute(
                """
                SELECT id, order_index, title, description
                FROM story_beats WHERE project_id = ?
                ORDER BY order_index, id
                """,
                (project_id,),
            ).fetchall()
            exact = [r for r in rows if (r["title"] or "").strip().lower() == wanted]
            row = exact[0] if exact else None
        if row is None:
            return {"ok": False, "error": "beat not found"}
        return {"ok": True, "beat": row_to_dict(row)}
    finally:
        conn.close()


def list_scenes(project_id: int) -> dict[str, Any]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, order_index, heading, action
            FROM scenes WHERE project_id = ?
            ORDER BY order_index, id
            """,
            (project_id,),
        ).fetchall()
        scenes = []
        for row in rows:
            item = row_to_dict(row)
            action = item.get("action") or ""
            if len(action) > _ACTION_PREVIEW:
                item["action"] = action[:_ACTION_PREVIEW] + "…"
            scenes.append(item)
        return {"ok": True, "count": len(scenes), "scenes": scenes}
    finally:
        conn.close()


def get_scene(
    project_id: int,
    *,
    scene_id: int | None = None,
    order: int | None = None,
) -> dict[str, Any]:
    """One scene in full, including dialogue and its clips."""
    if scene_id is None and order is None:
        return {"ok": False, "error": "pass scene_id or order"}
    conn = _connect()
    try:
        if scene_id is not None:
            row = conn.execute(
                """
                SELECT id, order_index, heading, action, dialog, duration_sec
                FROM scenes WHERE id = ? AND project_id = ?
                """,
                (int(scene_id), project_id),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT id, order_index, heading, action, dialog, duration_sec
                FROM scenes WHERE project_id = ? AND order_index = ?
                """,
                (project_id, int(order)),
            ).fetchone()
        if row is None:
            return {"ok": False, "error": "scene not found"}
        scene = row_to_dict(row)
        clips = conn.execute(
            """
            SELECT id, order_index, description, shot_size, duration_sec,
                   workflow_id, video_settings_json
            FROM clips WHERE scene_id = ?
            ORDER BY order_index, id
            """,
            (scene["id"],),
        ).fetchall()
        scene["clips"] = []
        for clip in clips:
            item = row_to_dict(clip)
            raw = item.pop("video_settings_json", None) or "{}"
            try:
                settings_json = json.loads(raw)
            except json.JSONDecodeError:
                settings_json = {}
            if not isinstance(settings_json, dict):
                settings_json = {}
            item["prompt"] = settings_json.get("prompt_draft") or ""
            scene["clips"].append(item)
        return {"ok": True, "scene": scene}
    finally:
        conn.close()


def add_scene(
    project_id: int,
    *,
    heading: str | None = None,
    action: str | None = None,
    dialog: str | None = None,
    duration_sec: int | None = None,
    beat_id: int | None = None,
) -> dict[str, Any]:
    """Insert one scene and its default clip. Empty heading and action is not a write."""
    heading_text = (heading or "").strip()
    action_text = (action or "").strip()
    dialog_text = (dialog or "").strip()
    if not heading_text and not action_text:
        return {"ok": False, "error": "heading or action is required"}
    duration: int | None
    if duration_sec is None or duration_sec == "":
        duration = None
    else:
        try:
            duration = int(duration_sec)
        except (TypeError, ValueError):
            return {"ok": False, "error": "duration_sec must be an integer"}
    conn = _connect()
    try:
        if not conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone():
            return {"ok": False, "error": "project not found"}
        beat_value: int | None = None
        if beat_id is not None and beat_id != "":
            try:
                beat_value = int(beat_id)
            except (TypeError, ValueError):
                return {"ok": False, "error": "beat_id must be an integer"}
            owned = conn.execute(
                "SELECT id FROM story_beats WHERE id = ? AND project_id = ?",
                (beat_value, project_id),
            ).fetchone()
            if not owned:
                return {"ok": False, "error": "beat_id is not in this project"}
        current = conn.execute(
            "SELECT COALESCE(MAX(order_index), 0) FROM scenes WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        order_index = int(current[0]) + 1
        cur = conn.execute(
            """
            INSERT INTO scenes
                (project_id, beat_id, order_index, heading, action, dialog, duration_sec)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, beat_value, order_index, heading_text, action_text, dialog_text, duration),
        )
        scene_id = int(cur.lastrowid)
        ensure_default_clip(conn, scene_id, project_id)
        conn.commit()
        row = conn.execute(
            """
            SELECT id, order_index, heading, action, dialog, duration_sec, beat_id
            FROM scenes WHERE id = ?
            """,
            (scene_id,),
        ).fetchone()
        return {"ok": True, "scene": row_to_dict(row)}
    finally:
        conn.close()


def execute(name: str, arguments: dict[str, Any], project_id: int) -> dict[str, Any]:
    """Run one granted tool. Unknown names are the caller's problem."""
    args = arguments or {}
    if name == "list_beats":
        return list_beats(project_id)
    if name == "get_beat":
        return get_beat(project_id, order=args.get("order"), title=args.get("title"))
    if name == "list_scenes":
        return list_scenes(project_id)
    if name == "add_scene":
        return add_scene(
            project_id,
            heading=args.get("heading"),
            action=args.get("action"),
            dialog=args.get("dialog"),
            duration_sec=args.get("duration_sec"),
            beat_id=args.get("beat_id"),
        )
    return {"ok": False, "error": f"{name} is not a scene tool"}
