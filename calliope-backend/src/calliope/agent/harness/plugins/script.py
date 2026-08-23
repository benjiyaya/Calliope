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


def annotate_scene_row(s: dict[str, Any]) -> dict[str, Any]:
    """Label user-facing clip # vs database scene_id. #N on Video is order."""
    oid = int(s["id"])
    order = int(s.get("order_index") or 0)
    s["scene_id"] = oid
    s["order"] = order
    s["clip"] = f"#{order}"
    return s


def resolve_scene_ref(
    conn,
    project_id: int,
    *,
    scene_id: Any = None,
    order: Any = None,
) -> tuple[int | None, str | None]:
    """Resolve one scene. `order` is the Video page #N; `scene_id` is the DB id."""
    if scene_id is not None and str(scene_id).strip() != "":
        try:
            sid = int(scene_id)
        except (TypeError, ValueError):
            return None, "scene_id must be an integer from list_scenes"
        row = conn.execute(
            "SELECT id FROM scenes WHERE id = ? AND project_id = ?",
            (sid, project_id),
        ).fetchone()
        if not row:
            return None, (
                f"Scene {sid} not found in this project. "
                "That number is not the #N on the Video page — list_scenes "
                "and use scene_id or order."
            )
        return int(row["id"]), None
    if order is not None and str(order).strip() != "":
        try:
            num = int(order)
        except (TypeError, ValueError):
            return None, "order must be the clip number from the Video page (#1, #25)"
        row = conn.execute(
            "SELECT id FROM scenes WHERE project_id = ? AND order_index = ?",
            (project_id, num),
        ).fetchone()
        if not row:
            return None, (
                f"No clip #{num} in this project. "
                "Users say #N for order (list_scenes.order / clip), not scene_id."
            )
        return int(row["id"]), None
    return None, "Pass scene_id (database id) or order (Video page #N)"


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
                "List or search scenes. EACH row has scene_id (database id) AND "
                "order / clip (#N on the Video page). Users say '#25' or "
                "'scene 25' meaning order=25 — NEVER treat that as scene_id. "
                "Optional query (heading/action text) or orders (clip numbers) "
                "to search without dumping the whole timeline."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional heading/action search (user words)",
                    },
                    "orders": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Video page clip numbers (#1, #25) — not scene_id",
                    },
                },
            },
            executor=t_list_scenes,
            category="script",
        )
    )
    registry.register(
        ToolDefinition(
            name="update_scene",
            description=(
                "Update one existing scene. Identify it with scene_id "
                "(list_scenes.scene_id) OR order (Video page #N). Never guess. "
                "Pass only the fields to change."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "scene_id": {
                        "type": "integer",
                        "description": "Database id from list_scenes — not the #N on Video",
                    },
                    "order": {
                        "type": "integer",
                        "description": "Clip number from the Video page (#25 → 25)",
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
                "Delete a scene permanently. Identify with scene_id or order "
                "(Video page #N). Prefer update_scene for content changes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "scene_id": {
                        "type": "integer",
                        "description": "Database id from list_scenes",
                    },
                    "order": {
                        "type": "integer",
                        "description": "Clip number from the Video page (#N)",
                    },
                },
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
        scenes = [annotate_scene_row(row_to_dict(r)) for r in rows]
        raw_orders = args.get("orders") or []
        if raw_orders:
            want = set()
            for x in raw_orders:
                try:
                    want.add(int(x))
                except (TypeError, ValueError):
                    continue
            scenes = [s for s in scenes if int(s.get("order") or 0) in want]
        query = str(args.get("query") or "").strip().lower()
        if query:
            scenes = [
                s
                for s in scenes
                if query in (s.get("heading") or "").lower()
                or query in (s.get("action") or "").lower()
                or query in (s.get("dialog") or "").lower()
            ]
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
        scene_id, err = resolve_scene_ref(
            conn,
            ctx.project_id,
            scene_id=args.get("scene_id"),
            order=args.get("order"),
        )
        if err:
            return {"ok": False, "error": err}
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
        return {"scene": annotate_scene_row(row_to_dict(row))}
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
        return {"scene": annotate_scene_row(row_to_dict(row))}
    finally:
        conn.close()


async def t_delete_scene(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    conn = _db()
    try:
        scene_id, err = resolve_scene_ref(
            conn,
            ctx.project_id,
            scene_id=args.get("scene_id"),
            order=args.get("order"),
        )
        if err:
            return {"ok": False, "error": err}
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
        return {"scenes": [annotate_scene_row(row_to_dict(r)) for r in rows]}
    finally:
        conn.close()
