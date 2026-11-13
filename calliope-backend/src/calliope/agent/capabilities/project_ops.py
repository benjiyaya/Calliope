"""Edit the linked project, or link this chat to one that already exists."""

from __future__ import annotations

from typing import Any

from calliope.agent.capabilities.sql import as_int, connect
from calliope.authoring.projects import update_project as write_project
from calliope.authoring.service import ValidationFailed
from calliope.db import row_to_dict
from calliope.events.bus import event_bus

_FIELDS = ("title", "idea", "genre", "tone", "target_duration")


def update_project(project_id: int, args: dict[str, Any]) -> dict[str, Any]:
    fields = {key: args[key] for key in _FIELDS if args.get(key) is not None}
    if "title" in fields:
        fields["title"] = str(fields["title"]).strip()[:200]
    if not fields:
        return {"ok": False, "error": "nothing to update"}
    conn = connect()
    try:
        try:
            row = write_project(conn, project_id, fields)
        except ValidationFailed as exc:
            return {"ok": False, "error": str(exc)}
        conn.commit()
        return {"ok": True, "project_id": project_id, "project": row}
    finally:
        conn.close()


async def link_project(session_id: int | None, args: dict[str, Any]) -> dict[str, Any]:
    project_id = as_int(args.get("project_id") or args.get("id"))
    if project_id is None:
        return {"ok": False, "error": "project_id is required"}
    if session_id is None:
        return {"ok": False, "error": "no session"}
    conn = connect()
    try:
        project = conn.execute(
            "SELECT id, title, status FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if project is None:
            return {"ok": False, "error": "project not found"}
        session = conn.execute(
            "SELECT * FROM agent_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if session is None:
            return {"ok": False, "error": "session not found"}
        current = session["project_id"]
        if current is not None and int(current) != project_id:
            return {"ok": False, "error": "this chat is already linked to another project"}
        if current is None:
            conn.execute(
                """
                UPDATE agent_sessions
                SET project_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (project_id, session_id),
            )
        canvas = conn.execute(
            "SELECT id FROM canvas WHERE project_id = ? ORDER BY id LIMIT 1",
            (project_id,),
        ).fetchone()
        if canvas is None:
            title = str(project["title"] or "Project")[:60]
            cur = conn.execute(
                """
                INSERT INTO canvas (project_id, title, created_at, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (project_id, title),
            )
            canvas_id = int(cur.lastrowid)
        else:
            canvas_id = int(canvas["id"])
        conn.commit()
        row = conn.execute(
            "SELECT * FROM agent_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        payload = row_to_dict(row)
        payload["project"] = row_to_dict(project)
        payload["running"] = True
    finally:
        conn.close()
    await event_bus.publish("agent.session.updated", {"session": payload})
    return {
        "ok": True,
        "project_id": project_id,
        "canvas_id": canvas_id,
        "title": project["title"],
    }
