"""Story generation used by the story grant. The HTTP route keeps its own chunk loop."""

from __future__ import annotations

from typing import Any

from calliope.agent.capabilities.sql import connect, touch
from calliope.agent.idea_guard import refuse_long_idea
from calliope.agent.llm import generate_structured
from calliope.agent.prompts import build_story_messages
from calliope.db import row_to_dict


async def generate_story_board(project_id: int, *, replace: bool = False) -> dict[str, Any]:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "project not found"}
        project = row_to_dict(row)
    finally:
        conn.close()
    refuse_long_idea(project, generator="storyline generator")
    data = await generate_structured(
        build_story_messages(
            title=project.get("title"),
            idea=project.get("idea"),
            genre=project.get("genre"),
            tone=project.get("tone"),
            target_duration=project.get("target_duration"),
        )
    )
    conn = connect()
    try:
        if replace:
            conn.execute("DELETE FROM story_beats WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM characters WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM locations WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM items WHERE project_id = ?", (project_id,))
        for index, beat in enumerate(data.get("beats") or [], start=1):
            title = str(beat.get("title") or "").strip()
            if not title:
                continue
            conn.execute(
                """
                INSERT INTO story_beats (project_id, order_index, title, description)
                VALUES (?, ?, ?, ?)
                """,
                (project_id, int(beat.get("order_index") or index), title, beat.get("description")),
            )
        for character in data.get("characters") or []:
            name = str(character.get("name") or "").strip()
            if not name:
                continue
            conn.execute(
                """
                INSERT INTO characters (project_id, name, role, age, appearance, personality)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    name,
                    character.get("role"),
                    character.get("age"),
                    character.get("appearance"),
                    character.get("personality"),
                ),
            )
        for location in data.get("locations") or []:
            name = str(location.get("name") or "").strip()
            if not name:
                continue
            conn.execute(
                "INSERT INTO locations (project_id, name, description) VALUES (?, ?, ?)",
                (project_id, name, location.get("description")),
            )
        for item in data.get("items") or []:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            conn.execute(
                "INSERT INTO items (project_id, name, description) VALUES (?, ?, ?)",
                (project_id, name, item.get("description")),
            )
        if data.get("title"):
            conn.execute(
                "UPDATE projects SET title = ? WHERE id = ?",
                (data["title"], project_id),
            )
        touch(conn, project_id)
        conn.commit()
        beats = conn.execute(
            "SELECT COUNT(*) FROM story_beats WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        beat_count = int(beats)
    finally:
        conn.close()
    from calliope.events.bus import event_bus

    await event_bus.publish(
        "story.ready",
        {"project_id": project_id, "message": f"Story drafted — {beat_count} beats"},
    )
    return {"ok": True, "beats": beat_count, "title": data.get("title"), "project_id": project_id}
