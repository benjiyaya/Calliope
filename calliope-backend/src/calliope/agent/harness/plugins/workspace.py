"""Workspace plugin: session linkage, project bootstrap, project metadata."""
from __future__ import annotations

from typing import Any

from calliope.agent.harness.registry import (
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    _db,
    _project_or_error,
    _project_stats,
    publish_session_updated,
)
from calliope.db import row_to_dict


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="get_workspace",
            description=(
                "Get the current workspace: session linkage and, when linked, "
                "the project with story beats, characters, locations, items, "
                "scenes, and stats. ALWAYS call this before editing anything or "
                "when you need current ids/counts. On a LARGE project the full "
                "result gets truncated — pass sections (e.g. "
                "[\"characters\",\"locations\",\"items\"]) to fetch only what "
                "you need; project + stats are always included."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "sections": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "beats",
                                "characters",
                                "locations",
                                "items",
                                "scenes",
                            ],
                        },
                        "description": (
                            "Which sections to include. Omit for all — but on "
                            "large projects prefer only the sections you need."
                        ),
                    }
                },
            },
            executor=t_get_workspace,
            requires_project=False,
            category="workspace",
        )
    )
    registry.register(
        ToolDefinition(
            name="link_project",
            description=(
                "Link this sandbox session to an EXISTING project (call "
                "list_projects first to get valid ids). Only possible while the "
                "session is not yet linked — to switch projects, call "
                "unlink_project first. Do NOT create a duplicate project "
                "when one already matches."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "integer",
                        "description": "ID of the project to attach to",
                    }
                },
                "required": ["project_id"],
            },
            executor=t_link_project,
            requires_project=False,
            blind_only=True,
            category="workspace",
        )
    )
    registry.register(
        ToolDefinition(
            name="unlink_project",
            description=(
                "Detach this session from its linked project and return to "
                "sandbox mode. The project and all its content are untouched — "
                "only the session binding is cleared. Use this when the user "
                "wants a NEW project (then create_project) or a DIFFERENT "
                "existing one (then link_project). Not available in sandbox."
            ),
            parameters={"type": "object", "properties": {}},
            executor=t_unlink_project,
            category="workspace",
        )
    )
    registry.register(
        ToolDefinition(
            name="create_project",
            description=(
                "Create a NEW film project (story/script/assets). Do NOT use "
                "this for a tagged @workflow image/video generate — call "
                "run_workflow instead; sandbox stays unlinked. Use only when "
                "the user wants a film and nothing existing matches (check "
                "list_projects first). Automatically links this session."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short project title"},
                    "idea": {
                        "type": "string",
                        "description": "One-paragraph story idea / logline",
                    },
                    "genre": {"type": "string"},
                    "tone": {"type": "string"},
                    "target_duration": {
                        "type": "string",
                        "description": "Target video duration, e.g. '60s', '3min', '90 seconds'",
                    },
                },
                "required": ["title", "idea"],
            },
            executor=t_create_project,
            requires_project=False,
            blind_only=True,
            category="workspace",
        )
    )
    registry.register(
        ToolDefinition(
            name="update_project",
            description=(
                "Update the linked project's metadata (title, idea, genre, "
                "tone, target_duration). Pass only the fields to change."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "idea": {"type": "string"},
                    "genre": {"type": "string"},
                    "tone": {"type": "string"},
                    "target_duration": {"type": "string"},
                },
            },
            executor=t_update_project,
            category="project",
        )
    )
    registry.register(
        ToolDefinition(
            name="list_projects",
            description=(
                "List existing projects (id, title, stats) — call this BEFORE "
                "create_project so you never duplicate an existing project, "
                "and to find the id for link_project."
            ),
            parameters={"type": "object", "properties": {}},
            executor=t_list_projects,
            requires_project=False,
            blind_only=True,
            category="workspace",
        )
    )


_WS_SECTIONS = ("beats", "characters", "locations", "items", "scenes")


# ── executors ───────────────────────────────────────────────────


async def t_get_workspace(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    conn = _db()
    try:
        session = conn.execute(
            "SELECT id, project_id, title, status FROM agent_sessions WHERE id = ?",
            (ctx.session_id,),
        ).fetchone()
        if not session:
            return {"ok": False, "error": "Session not found"}
        out: dict[str, Any] = {"session": row_to_dict(session)}
        if ctx.project_id is None:
            out["mode"] = "sandbox"
            out["hint"] = (
                "No project linked. For @workflow image/video generates, call "
                "run_workflow (Playground scratch). To file a generated image "
                "onto an existing film, list_projects then attach_asset. Only "
                "create_project when the user wants a new film."
            )
            return out
        project = _project_or_error(conn, ctx.project_id)
        if not project:
            out["mode"] = "sandbox"
            out["hint"] = "Linked project no longer exists."
            return out
        requested = args.get("sections")
        if requested:
            wanted = {sec for sec in requested if sec in _WS_SECTIONS}
            if not wanted:
                return {
                    "ok": False,
                    "error": f"No valid sections in {requested!r}; valid: {sorted(_WS_SECTIONS)}",
                }
        else:
            wanted = set(_WS_SECTIONS)
        beats = [
            row_to_dict(r)
            for r in conn.execute(
                "SELECT id, order_index, title, description FROM story_beats "
                "WHERE project_id = ? ORDER BY order_index",
                (ctx.project_id,),
            ).fetchall()
        ] if "beats" in wanted else None
        characters = [
            row_to_dict(r)
            for r in conn.execute(
                "SELECT id, name, role, age, appearance, personality, portrait_path, "
                "sheet_path, consistency_prompt FROM characters WHERE project_id = ?",
                (ctx.project_id,),
            ).fetchall()
        ] if "characters" in wanted else None
        locations = [
            row_to_dict(r)
            for r in conn.execute(
                "SELECT id, name, description, reference_image_path, consistency_prompt "
                "FROM locations WHERE project_id = ?",
                (ctx.project_id,),
            ).fetchall()
        ] if "locations" in wanted else None
        items = [
            row_to_dict(r)
            for r in conn.execute(
                "SELECT id, name, description, reference_image_path, consistency_prompt "
                "FROM items WHERE project_id = ?",
                (ctx.project_id,),
            ).fetchall()
        ] if "items" in wanted else None
        scenes = [
            row_to_dict(r)
            for r in conn.execute(
                "SELECT id, order_index, heading, action, dialog, duration_sec, "
                "location_id, video_path FROM scenes WHERE project_id = ? ORDER BY order_index",
                (ctx.project_id,),
            ).fetchall()
        ] if "scenes" in wanted else None
        out["mode"] = "linked"
        out["project"] = project
        out["stats"] = _project_stats(ctx.project_id, conn)
        for key, value in (
            ("beats", beats),
            ("characters", characters),
            ("locations", locations),
            ("items", items),
            ("scenes", scenes),
        ):
            if value is not None:
                out[key] = value
        if len(wanted) < len(_WS_SECTIONS):
            out["sections_omitted"] = sorted(set(_WS_SECTIONS) - wanted)
        return out
    finally:
        conn.close()


async def t_link_project(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.project_id is not None:
        return {"ok": False, "error": "Session already linked to a project."}
    pid = int(args["project_id"])
    conn = _db()
    try:
        project = _project_or_error(conn, pid)
        if not project:
            return {"ok": False, "error": f"Project {pid} not found"}
        conn.execute(
            "UPDATE agent_sessions SET project_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (pid, ctx.session_id),
        )
        conn.commit()
    finally:
        conn.close()
    ctx.project_id = pid
    await publish_session_updated(ctx.session_id)
    return {"ok": True, "project_id": pid, "title": project["title"]}


async def t_unlink_project(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    released_id = ctx.project_id
    conn = _db()
    try:
        project = _project_or_error(conn, released_id)
        conn.execute(
            "UPDATE agent_sessions SET project_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (ctx.session_id,),
        )
        conn.commit()
    finally:
        conn.close()
    ctx.project_id = None
    await publish_session_updated(ctx.session_id)
    return {
        "ok": True,
        "released_project_id": released_id,
        "released_title": (project or {}).get("title"),
        "mode": "sandbox",
        "hint": "Session is back in sandbox — create_project and link_project are available again.",
    }


_TITLE_MAX = 200  # mirrors ProjectCreate/ProjectUpdate schema bounds


async def t_create_project(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    title = args.get("title")
    if not isinstance(title, str) or not title.strip():
        # str(None) would create a project literally named "None".
        return {"ok": False, "error": "title is required (non-empty string)"}
    if len(title) > _TITLE_MAX:
        return {"ok": False, "error": f"title too long (max {_TITLE_MAX} characters)"}
    conn = _db()
    try:
        cur = conn.execute(
            """
            INSERT INTO projects (title, idea, genre, tone, target_duration)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                title.strip(),
                args.get("idea"),
                args.get("genre"),
                args.get("tone"),
                args.get("target_duration"),
            ),
        )
        conn.commit()
        pid = cur.lastrowid
        project = _project_or_error(conn, pid)
    finally:
        conn.close()
    if ctx.project_id is None:
        # Sandbox → linked: materialize the workspace binding.
        conn = _db()
        try:
            conn.execute(
                "UPDATE agent_sessions SET project_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (pid, ctx.session_id),
            )
            conn.commit()
        finally:
            conn.close()
        ctx.project_id = pid
        await publish_session_updated(ctx.session_id)
    return {"ok": True, "project": project}


async def t_update_project(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    conn = _db()
    try:
        # Whitelist column names — arg keys come from the LLM and must never
        # reach SQL as identifiers.
        allowed = ("title", "idea", "genre", "tone", "target_duration")
        data = {k: v for k, v in args.items() if k in allowed and v is not None}
        if "title" in data and len(str(data["title"])) > _TITLE_MAX:
            return {
                "ok": False,
                "error": f"title too long (max {_TITLE_MAX} characters)",
            }
        if data:
            fields = ", ".join(f"{k} = :{k}" for k in data)
            data["id"] = ctx.project_id
            conn.execute(
                f"UPDATE projects SET {fields}, updated_at = CURRENT_TIMESTAMP WHERE id = :id",
                data,
            )
            conn.commit()
        return {"project": _project_or_error(conn, ctx.project_id)}
    finally:
        conn.close()


async def t_list_projects(ctx: ToolContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM projects WHERE status != 'system' ORDER BY updated_at DESC"
        ).fetchall()
        return [row_to_dict(r) | {"stats": _project_stats(r["id"], conn)} for r in rows]
    finally:
        conn.close()
