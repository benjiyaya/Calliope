"""Add, update, and delete story beats."""

from __future__ import annotations

from typing import Any

from calliope.agent.capabilities.sql import as_int, connect, touch
from calliope.db import row_to_dict


def add_beat(project_id: int, args: dict[str, Any]) -> dict[str, Any]:
    title = args.get("title")
    if not isinstance(title, str) or not title.strip():
        return {"ok": False, "error": "title is required"}
    conn = connect()
    try:
        max_idx = conn.execute(
            "SELECT COALESCE(MAX(order_index), 0) FROM story_beats WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0]
        idx = as_int(args.get("order_index"))
        idx = max_idx + 1 if idx is None else max(1, min(idx, int(max_idx) + 1))
        conn.execute(
            "UPDATE story_beats SET order_index = order_index + 1 "
            "WHERE project_id = ? AND order_index >= ?",
            (project_id, idx),
        )
        cur = conn.execute(
            "INSERT INTO story_beats (project_id, order_index, title, description) "
            "VALUES (?, ?, ?, ?)",
            (project_id, idx, title.strip(), args.get("description")),
        )
        touch(conn, project_id)
        conn.commit()
        beat = conn.execute(
            "SELECT id, order_index, title, description FROM story_beats WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        return {"ok": True, "beat": row_to_dict(beat)}
    finally:
        conn.close()


def update_beat(project_id: int, args: dict[str, Any]) -> dict[str, Any]:
    beat_id = as_int(args.get("beat_id"))
    if beat_id is None:
        return {"ok": False, "error": "beat_id is required"}
    data = {
        key: args[key]
        for key in ("title", "description", "order_index")
        if args.get(key) is not None
    }
    if "title" in data and not str(data["title"]).strip():
        data.pop("title")
    conn = connect()
    try:
        existing = conn.execute(
            "SELECT id FROM story_beats WHERE id = ? AND project_id = ?",
            (beat_id, project_id),
        ).fetchone()
        if not existing:
            return {"ok": False, "error": f"Beat {beat_id} not found in this project"}
        if data:
            fields = ", ".join(f"{key} = :{key}" for key in data)
            conn.execute(
                f"UPDATE story_beats SET {fields} WHERE id = :id AND project_id = :project_id",
                {**data, "id": beat_id, "project_id": project_id},
            )
            touch(conn, project_id)
            conn.commit()
        beat = conn.execute(
            "SELECT id, order_index, title, description FROM story_beats WHERE id = ?",
            (beat_id,),
        ).fetchone()
        return {"ok": True, "beat": row_to_dict(beat)}
    finally:
        conn.close()


def delete_beat(project_id: int, args: dict[str, Any]) -> dict[str, Any]:
    beat_id = as_int(args.get("beat_id"))
    if beat_id is None:
        return {"ok": False, "error": "beat_id is required"}
    conn = connect()
    try:
        row = conn.execute(
            "SELECT order_index FROM story_beats WHERE id = ? AND project_id = ?",
            (beat_id, project_id),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"Beat {beat_id} not found in this project"}
        conn.execute(
            "DELETE FROM story_beats WHERE id = ? AND project_id = ?",
            (beat_id, project_id),
        )
        conn.execute(
            "UPDATE story_beats SET order_index = order_index - 1 "
            "WHERE project_id = ? AND order_index > ?",
            (project_id, row["order_index"]),
        )
        touch(conn, project_id)
        conn.commit()
        return {"ok": True, "deleted_beat_id": beat_id}
    finally:
        conn.close()
