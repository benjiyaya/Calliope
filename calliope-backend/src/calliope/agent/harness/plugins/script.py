"""Script plugin: scene generation + per-scene CRUD."""
from __future__ import annotations

from typing import Any

from calliope.agent.harness.registry import (
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    _db,
)
from calliope.db import row_to_dict


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="generate_script",
            description=(
                "Generate the scene script for the linked project via the script "
                "agent. DESTRUCTIVE: replace=true (default) DELETES all existing "
                "scenes first. If the user only wants tweaks, use update_scene / "
                "add_scene instead."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "scene_count": {"type": "integer", "minimum": 1},
                    "replace": {
                        "type": "boolean",
                        "description": "Replace existing scenes (default true = destructive)",
                    },
                },
            },
            executor=t_generate_script,
            category="script",
            destructive=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="list_scenes",
            description=(
                "List the linked project's scenes in order with their real ids, "
                "cast, and location — the source of truth for scene_id arguments."
            ),
            parameters={"type": "object", "properties": {}},
            executor=t_list_scenes,
            category="script",
        )
    )
    registry.register(
        ToolDefinition(
            name="update_scene",
            description=(
                "Update one existing scene. scene_id must come from list_scenes "
                "or get_workspace — never guess it. Pass only the fields to change."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "scene_id": {
                        "type": "integer",
                        "description": "Real id from list_scenes/get_workspace",
                    },
                    "heading": {"type": "string"},
                    "action": {"type": "string"},
                    "dialog": {"type": "string"},
                    "duration_sec": {"type": "integer", "minimum": 1},
                    "location_id": {"type": "integer", "description": "Set the scene's location"},
                    "character_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Replace the scene's character cast",
                    },
                },
                "required": ["scene_id"],
            },
            executor=t_update_scene,
            category="script",
        )
    )
    registry.register(
        ToolDefinition(
            name="add_scene",
            description=(
                "Append a new scene at the end of the script (or at insert_at "
                "position). location_id / character_ids must be real ids from "
                "get_workspace. Use this for targeted additions instead of "
                "regenerating the whole script."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "heading": {
                        "type": "string",
                        "description": "Scene heading, e.g. INT. LOCATION - TIME",
                    },
                    "action": {"type": "string", "description": "What happens in the scene"},
                    "dialog": {"type": "string"},
                    "duration_sec": {"type": "integer", "minimum": 1},
                    "location_id": {"type": "integer"},
                    "character_ids": {"type": "array", "items": {"type": "integer"}},
                    "insert_at": {
                        "type": "integer",
                        "description": "1-based position; omit to append at the end",
                    },
                },
            },
            executor=t_add_scene,
            category="script",
        )
    )
    registry.register(
        ToolDefinition(
            name="delete_scene",
            description=(
                "Delete a scene permanently. scene_id must come from list_scenes. "
                "Prefer update_scene for content changes; use this only when the "
                "user wants the scene gone."
            ),
            parameters={
                "type": "object",
                "properties": {"scene_id": {"type": "integer"}},
                "required": ["scene_id"],
            },
            executor=t_delete_scene,
            category="script",
            destructive=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="reorder_scenes",
            description=(
                "Reorder scenes: pass the COMPLETE ordered list of all real scene "
                "ids (from list_scenes). Missing ids keep their relative order at "
                "the end."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "scene_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["scene_ids"],
            },
            executor=t_reorder_scenes,
            category="script",
        )
    )


# ── executors ───────────────────────────────────────────────────


async def t_generate_script(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from calliope.agent.script_agent import generate_script as _gen

    scene_count = args.get("scene_count")
    return await _gen(
        ctx.project_id,
        replace=bool(args.get("replace", True)),
        scene_count=int(scene_count) if scene_count else None,
    )


async def t_list_scenes(ctx: ToolContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM scenes WHERE project_id = ? ORDER BY order_index", (ctx.project_id,)
        ).fetchall()
        scenes = [row_to_dict(r) for r in rows]
        for s in scenes:
            chars = conn.execute(
                """
                SELECT c.id, c.name FROM characters c
                JOIN scene_characters sc ON sc.character_id = c.id
                WHERE sc.scene_id = ?
                """,
                (s["id"],),
            ).fetchall()
            s["characters"] = [row_to_dict(c) for c in chars]
        return scenes
    finally:
        conn.close()


async def t_update_scene(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    conn = _db()
    try:
        scene_id = int(args["scene_id"])
        existing = conn.execute(
            "SELECT * FROM scenes WHERE id = ? AND project_id = ?",
            (scene_id, ctx.project_id),
        ).fetchone()
        if not existing:
            return {"ok": False, "error": f"Scene {scene_id} not found in this project"}
        # Validate location ownership before writing: a foreign location_id
        # would both dangle and (via env seeding below) import another
        # project's reference image into this scene.
        if args.get("location_id") is not None:
            loc = conn.execute(
                "SELECT id FROM locations WHERE id = ? AND project_id = ?",
                (args["location_id"], ctx.project_id),
            ).fetchone()
            if not loc:
                return {
                    "ok": False,
                    "error": f"Location {args['location_id']} not found in this project",
                }
        cols = ("heading", "action", "dialog", "duration_sec", "location_id")
        data = {k: v for k, v in args.items() if k in cols and v is not None}
        if data:
            fields = ", ".join(f"{k} = :{k}" for k in data)
            data["id"] = scene_id
            data["pid"] = ctx.project_id
            # Moving a scene to a new location also seeds its env image from
            # that location's reference image (when not already set).
            extra = (
                ", env_image_path = COALESCE(env_image_path, "
                "(SELECT reference_image_path FROM locations WHERE id = :location_id))"
                if "location_id" in data
                else ""
            )
            conn.execute(
                f"UPDATE scenes SET {fields}{extra} WHERE id = :id AND project_id = :pid",
                data,
            )
        if "character_ids" in args and args["character_ids"] is not None:
            conn.execute("DELETE FROM scene_characters WHERE scene_id = ?", (scene_id,))
            for cid in args["character_ids"]:
                exists = conn.execute(
                    "SELECT id FROM characters WHERE id = ? AND project_id = ?",
                    (cid, ctx.project_id),
                ).fetchone()
                if exists:
                    conn.execute(
                        "INSERT OR IGNORE INTO scene_characters (scene_id, character_id) VALUES (?, ?)",
                        (scene_id, cid),
                    )
        conn.execute(
            "UPDATE projects SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (ctx.project_id,),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM scenes WHERE id = ? AND project_id = ?",
            (scene_id, ctx.project_id),
        ).fetchone()
        return {"scene": row_to_dict(row)}
    finally:
        conn.close()


async def t_add_scene(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    conn = _db()
    try:
        existing = conn.execute(
            "SELECT id FROM scenes WHERE project_id = ? ORDER BY order_index",
            (ctx.project_id,),
        ).fetchall()
        ids = [r["id"] for r in existing]
        env_path = None
        if args.get("location_id") is not None:
            loc = conn.execute(
                "SELECT reference_image_path FROM locations WHERE id = ? AND project_id = ?",
                (args["location_id"], ctx.project_id),
            ).fetchone()
            if not loc:
                # Reject foreign location ids instead of writing a dangling
                # reference (loc lookup is project-scoped).
                return {
                    "ok": False,
                    "error": f"Location {args['location_id']} not found in this project",
                }
            env_path = loc["reference_image_path"]
        cur = conn.execute(
            """
            INSERT INTO scenes
            (project_id, order_index, heading, action, dialog, duration_sec, location_id, env_image_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ctx.project_id,
                len(ids) + 1,
                args.get("heading"),
                args.get("action"),
                args.get("dialog"),
                args.get("duration_sec"),
                args.get("location_id"),
                env_path,
            ),
        )
        scene_id = cur.lastrowid
        for cid in args.get("character_ids") or []:
            exists = conn.execute(
                "SELECT id FROM characters WHERE id = ? AND project_id = ?",
                (cid, ctx.project_id),
            ).fetchone()
            if exists:
                conn.execute(
                    "INSERT OR IGNORE INTO scene_characters (scene_id, character_id) VALUES (?, ?)",
                    (scene_id, cid),
                )
        conn.execute(
            "UPDATE projects SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (ctx.project_id,),
        )
        conn.commit()
        if args.get("insert_at"):
            pos = int(args["insert_at"])
            if 1 <= pos <= len(ids) + 1:
                new_ids = [i for i in ids if i != scene_id]
                new_ids.insert(pos - 1, scene_id)
                for idx, sid in enumerate(new_ids, start=1):
                    conn.execute(
                        "UPDATE scenes SET order_index = ? WHERE id = ?", (idx, sid)
                    )
                conn.commit()
        row = conn.execute("SELECT * FROM scenes WHERE id = ?", (scene_id,)).fetchone()
        return {"scene": row_to_dict(row)}
    finally:
        conn.close()


async def t_delete_scene(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    conn = _db()
    try:
        scene_id = int(args["scene_id"])
        cur = conn.execute(
            "DELETE FROM scenes WHERE id = ? AND project_id = ?", (scene_id, ctx.project_id)
        )
        conn.execute(
            "UPDATE projects SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (ctx.project_id,)
        )
        conn.commit()
        if cur.rowcount == 0:
            return {"ok": False, "error": f"Scene {scene_id} not found"}
        return {"ok": True}
    finally:
        conn.close()


async def t_reorder_scenes(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    conn = _db()
    try:
        ids = [int(i) for i in args["scene_ids"]]
        for index, sid in enumerate(ids, start=1):
            conn.execute(
                "UPDATE scenes SET order_index = ? WHERE id = ? AND project_id = ?",
                (index, sid, ctx.project_id),
            )
        conn.commit()
        rows = conn.execute(
            "SELECT id, order_index, heading FROM scenes WHERE project_id = ? ORDER BY order_index",
            (ctx.project_id,),
        ).fetchall()
        return {"scenes": [row_to_dict(r) for r in rows]}
    finally:
        conn.close()
