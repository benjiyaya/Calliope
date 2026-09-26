"""Script plugin: scene generation + per-scene CRUD."""
from __future__ import annotations

import json
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
                "agent, then break every scene into shot clips (with_clips, "
                "default true). DESTRUCTIVE: replace=true (default) DELETES all "
                "existing scenes first. If the user only wants tweaks, use "
                "update_scene / add_scene instead."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "scene_count": {"type": "integer", "minimum": 1},
                    "with_clips": {
                        "type": "boolean",
                        "description": (
                            "Also break every scene into shot clips (default true — "
                            "one call produces the full script + shot list)"
                        ),
                    },
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
                "to search without dumping the whole timeline. Each scene also "
                "reports its clip count (scenes expand into 1+ shot clips)."
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
            name="list_clips",
            description=(
                "List shot clips under the project's scenes (a scene expands "
                "into 1+ clips; each clip is one ~5-10s video render). Every "
                "clip has clip_id (database id) and label '#<scene>.<clip>' "
                "(e.g. #3.2 = scene 3, clip 2) — the #N.M form users say. "
                "Optionally filter by scene_ids / orders (scene numbers) or "
                "refs ('#3.2')."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "scene_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Database scene ids (from list_scenes)",
                    },
                    "orders": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Scene order numbers (#3 → all clips of scene 3)",
                    },
                    "refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Clip labels like '#3.2'",
                    },
                },
            },
            executor=t_list_clips,
            category="script",
        )
    )
    registry.register(
        ToolDefinition(
            name="break_into_shots",
            description=(
                "Break script scene(s) into shot clips via the coverage pass "
                "(LLM allocates every dialogue line and action beat across "
                "~5-10s clips, preserving full content). Use after "
                "generate_script so long scenes get real coverage. Scope: pass "
                "scene_ids / orders, or all_scenes=true ONLY when the user "
                "asked for the whole script. Existing clips of those scenes "
                "are replaced."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "scene_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Database scene ids (from list_scenes)",
                    },
                    "orders": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Scene order numbers (#3, #7)",
                    },
                    "all_scenes": {
                        "type": "boolean",
                        "description": "Expand EVERY scene (bulk — only on explicit ask)",
                    },
                    "guidance": {
                        "type": "string",
                        "description": "Optional style/direction hints for the split",
                    },
                },
            },
            executor=t_break_into_shots,
            category="script",
            destructive=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="refresh_continuity_plan",
            description=(
                "Write or refresh the project's continuity ledger: one timed "
                "plan for every clip (style, speaker bindings, camera, lighting, "
                "what changes after each shot). Call this after generate_script "
                "or break_into_shots, before rendering, so later clip prompts "
                "are slices of the same film. force=true rewrites even when the "
                "board hash still matches."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "force": {
                        "type": "boolean",
                        "description": "Rewrite the plan even if the board hash matches (default true)",
                    },
                },
            },
            executor=t_refresh_continuity_plan,
            category="script",
        )
    )
    registry.register(
        ToolDefinition(
            name="add_clip",
            description=(
                "Add one shot clip to a scene's end (or order_index within it). "
                "description = what the camera sees + which of the scene's "
                "dialogue lines it performs; shot_size like 'wide' / 'medium' / "
                "'close-up'; duration_sec 3-15 (videos render ~5-10s)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "scene_id": {"type": "integer"},
                    "order": {
                        "type": "integer",
                        "description": "Scene order number (#3) instead of scene_id",
                    },
                    "order_index": {
                        "type": "integer",
                        "description": "Position within the scene (default: append)",
                    },
                    "description": {"type": "string"},
                    "shot_size": {"type": "string"},
                    "dialog_lines_covered": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "1-based dialogue line numbers this clip performs",
                    },
                    "duration_sec": {"type": "number"},
                    "workflow_id": {"type": "integer"},
                    "chain_from_prev": {"type": "boolean"},
                },
            },
            executor=t_add_clip,
            category="script",
        )
    )
    registry.register(
        ToolDefinition(
            name="update_clip",
            description=(
                "Edit one clip (description / shot_size / dialog_lines_covered / "
                "duration_sec / workflow_id / chain_from_prev). Address it by "
                "clip_id (from list_clips) or ref '#3.2'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "clip_id": {"type": "integer"},
                    "ref": {
                        "type": "string",
                        "description": "Clip label like '#3.2' instead of clip_id",
                    },
                    "description": {"type": "string"},
                    "shot_size": {"type": "string"},
                    "dialog_lines_covered": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "duration_sec": {"type": "number"},
                    "workflow_id": {"type": "integer"},
                    "chain_from_prev": {"type": "boolean"},
                },
            },
            executor=t_update_clip,
            category="script",
        )
    )
    registry.register(
        ToolDefinition(
            name="delete_clip",
            description=(
                "Delete one clip (by clip_id or ref '#3.2') and renumber its "
                "scene's remaining clips. The scene's LAST clip is protected — "
                "delete the scene itself instead."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "clip_id": {"type": "integer"},
                    "ref": {"type": "string", "description": "Clip label like '#3.2'"},
                },
            },
            executor=t_delete_clip,
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
        with_clips=bool(args.get("with_clips", True)),
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
            s["clip_count"] = conn.execute(
                "SELECT COUNT(*) AS n FROM clips WHERE scene_id = ?", (s["id"],)
            ).fetchone()["n"]
            s["clips_ready"] = conn.execute(
                "SELECT COUNT(*) AS n FROM clips WHERE scene_id = ? AND clip_path IS NOT NULL",
                (s["id"],),
            ).fetchone()["n"]
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


# ── Clips: the shot layer under each scene ──────────────────────────────


def _annotate_clip(c: dict[str, Any], scene_order: int) -> dict[str, Any]:
    """User-facing labels: clip_id (db id) + label '#<scene>.<clip>'."""
    c["clip_id"] = int(c["id"])
    c["label"] = f"#{scene_order}.{c['order_index']}"
    return c


def _resolve_clip_ref(
    conn,
    project_id: int,
    *,
    clip_id: Any = None,
    ref: Any = None,
) -> tuple[int | None, str | None]:
    """Resolve one clip. `ref` is the '#3.2' label form users say."""
    if clip_id is not None and str(clip_id).strip() != "":
        try:
            cid = int(clip_id)
        except (TypeError, ValueError):
            return None, "clip_id must be an integer from list_clips"
        row = conn.execute(
            "SELECT id FROM clips WHERE id = ? AND project_id = ?",
            (cid, project_id),
        ).fetchone()
        if not row:
            return None, f"Clip {clip_id} not found in this project — list_clips first"
        return int(row["id"]), None
    if ref is not None and str(ref).strip() != "":
        text = str(ref).strip().lstrip("#")
        parts = text.split(".")
        if len(parts) != 2:
            return None, "ref must look like '#3.2' (scene.clip) — list_clips first"
        try:
            s_pos, c_pos = int(parts[0]), int(parts[1])
        except ValueError:
            return None, "ref must look like '#3.2' (scene.clip) — list_clips first"
        row = conn.execute(
            """
            SELECT c.id FROM clips c JOIN scenes s ON s.id = c.scene_id
            WHERE c.project_id = ? AND s.order_index = ? AND c.order_index = ?
            """,
            (project_id, s_pos, c_pos),
        ).fetchone()
        if not row:
            return None, f"No clip #{s_pos}.{c_pos} in this project — list_clips first"
        return int(row["id"]), None
    return None, "Pass clip_id (from list_clips) or ref ('#3.2')"


async def t_list_clips(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    conn = _db()
    try:
        rows = conn.execute(
            """
            SELECT c.*, s.order_index AS scene_order, s.heading
            FROM clips c JOIN scenes s ON s.id = c.scene_id
            WHERE c.project_id = ?
            ORDER BY s.order_index, c.order_index, c.id
            """,
            (ctx.project_id,),
        ).fetchall()
        clips = [
            _annotate_clip(row_to_dict(r), int(r["scene_order"])) for r in rows
        ]
        scene_ids = [int(i) for i in (args.get("scene_ids") or [])]
        if scene_ids:
            want = set()
            for sid in scene_ids:
                found, err = resolve_scene_ref(conn, ctx.project_id, scene_id=sid)
                if err:
                    return {"ok": False, "error": err}
                want.add(found)
            clips = [c for c in clips if c["scene_id"] in want]
        refs = [str(r) for r in (args.get("refs") or []) if str(r).strip()]
        if refs:
            want = set()
            for ref in refs:
                found, err = _resolve_clip_ref(conn, ctx.project_id, ref=ref)
                if err:
                    return {"ok": False, "error": err}
                want.add(found)
            clips = [c for c in clips if c["id"] in want]
        return {"clips": clips, "count": len(clips)}
    finally:
        conn.close()


async def t_break_into_shots(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from calliope.agent.coverage_agent import expand_scene_coverage
    from calliope.agent.harness import log as session_log
    from calliope.agent.harness.policy import allows_bulk_video_enqueue

    scene_ids = [int(i) for i in (args.get("scene_ids") or [])]
    orders = [int(i) for i in (args.get("orders") or [])]
    all_scenes = bool(args.get("all_scenes"))
    guidance = str(args.get("guidance") or "").strip() or None

    if not scene_ids and not orders and not all_scenes:
        return {
            "ok": False,
            "error": (
                "Say which scenes to break: pass scene_ids / orders from "
                "list_scenes, or all_scenes=true only when the user asked for "
                "the whole script."
            ),
        }
    conn = _db()
    try:
        resolved: list[int] = []
        for sid in scene_ids:
            found, err = resolve_scene_ref(conn, ctx.project_id, scene_id=sid)
            if err:
                return {"ok": False, "error": err}
            resolved.append(found)
        for order in orders:
            found, err = resolve_scene_ref(conn, ctx.project_id, order=order)
            if err:
                return {"ok": False, "error": err}
            resolved.append(found)
        if all_scenes:
            resolved = [
                int(r["id"])
                for r in conn.execute(
                    "SELECT id FROM scenes WHERE project_id = ? ORDER BY order_index",
                    (ctx.project_id,),
                ).fetchall()
            ]
    finally:
        conn.close()
    seen: set[int] = set()
    resolved = [s for s in resolved if not (s in seen or seen.add(s))]
    if not resolved:
        return {"ok": False, "error": "No matching scenes to expand"}

    latest = session_log.latest_user_message(ctx.session_id) or ""
    if len(resolved) > 3 and not allows_bulk_video_enqueue(latest, len(resolved)):
        return {
            "ok": False,
            "error": (
                f"Breaking {len(resolved)} scenes is a bulk pass (~{len(resolved) * 5}+ "
                "clips). Confirm the user asked for every scene, or name the ones they want."
            ),
        }

    try:
        result = await expand_scene_coverage(
            ctx.project_id, resolved, guidance=guidance
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    total = sum(r["clips"] for r in result.get("scenes", []))
    return {
        "ok": True,
        "scenes": result.get("scenes", []),
        "total_clips": total,
        "note": "list_clips to see them; enqueue_video_jobs to render specific ones.",
    }


async def t_refresh_continuity_plan(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Rewrite the project continuity ledger from the current board."""
    from calliope.agent.continuity import ensure_continuity_plan

    if not ctx.project_id:
        return {"ok": False, "error": "Link a project before refreshing the continuity plan"}
    force = bool(args.get("force", True))
    plan = await ensure_continuity_plan(int(ctx.project_id), force=force)
    return {
        "ok": True,
        "based_on": plan.get("based_on") or "",
        "shots": len(plan.get("shots") or []),
        "refreshed": bool(plan.get("refreshed")),
    }


async def t_add_clip(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
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
        next_index = conn.execute(
            "SELECT COALESCE(MAX(order_index), 0) + 1 AS n FROM clips WHERE scene_id = ?",
            (scene_id,),
        ).fetchone()["n"]
        covered = args.get("dialog_lines_covered")
        covered_json = json.dumps([int(i) for i in covered]) if covered else None
        cur = conn.execute(
            """
            INSERT INTO clips (scene_id, project_id, order_index, description,
                               shot_size, dialog_lines_covered, duration_sec,
                               workflow_id, chain_from_prev)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scene_id,
                ctx.project_id,
                int(args.get("order_index") or next_index),
                args.get("description"),
                args.get("shot_size"),
                covered_json,
                args.get("duration_sec"),
                args.get("workflow_id"),
                1 if args.get("chain_from_prev") else 0,
            ),
        )
        conn.execute(
            "UPDATE projects SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (ctx.project_id,),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM clips WHERE id = ?", (cur.lastrowid,)).fetchone()
        scene = conn.execute(
            "SELECT order_index FROM scenes WHERE id = ?", (scene_id,)
        ).fetchone()
        return {"clip": _annotate_clip(row_to_dict(row), int(scene["order_index"]))}
    finally:
        conn.close()


async def t_update_clip(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    conn = _db()
    try:
        clip_id, err = _resolve_clip_ref(
            conn, ctx.project_id, clip_id=args.get("clip_id"), ref=args.get("ref")
        )
        if err:
            return {"ok": False, "error": err}
        cols = ("description", "shot_size", "duration_sec", "workflow_id", "chain_from_prev")
        data = {k: v for k, v in args.items() if k in cols and v is not None}
        if "dialog_lines_covered" in args and args["dialog_lines_covered"] is not None:
            data["dialog_lines_covered"] = json.dumps(
                [int(i) for i in args["dialog_lines_covered"]]
            )
        if not data:
            return {"ok": False, "error": "Nothing to update"}
        fields = ", ".join(f"{k} = :{k}" for k in data)
        data["id"] = clip_id
        data["pid"] = ctx.project_id
        conn.execute(
            f"UPDATE clips SET {fields} WHERE id = :id AND project_id = :pid", data
        )
        conn.execute(
            "UPDATE projects SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (ctx.project_id,),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT c.*, s.order_index AS scene_order FROM clips c
            JOIN scenes s ON s.id = c.scene_id WHERE c.id = ?
            """,
            (clip_id,),
        ).fetchone()
        return {"clip": _annotate_clip(row_to_dict(row), int(row["scene_order"]))}
    finally:
        conn.close()


async def t_delete_clip(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    conn = _db()
    try:
        clip_id, err = _resolve_clip_ref(
            conn, ctx.project_id, clip_id=args.get("clip_id"), ref=args.get("ref")
        )
        if err:
            return {"ok": False, "error": err}
        row = conn.execute(
            "SELECT scene_id, order_index FROM clips WHERE id = ?", (clip_id,)
        ).fetchone()
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM clips WHERE scene_id = ?", (row["scene_id"],)
        ).fetchone()["n"]
        if count <= 1:
            return {
                "ok": False,
                "error": "A scene needs at least one clip — delete_scene instead",
            }
        conn.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
        # Renumber remaining clips gaplessly
        remaining = conn.execute(
            "SELECT id FROM clips WHERE scene_id = ? ORDER BY order_index, id",
            (row["scene_id"],),
        ).fetchall()
        for idx, r in enumerate(remaining, start=1):
            conn.execute("UPDATE clips SET order_index = ? WHERE id = ?", (idx, r["id"]))
        conn.execute(
            "UPDATE projects SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (ctx.project_id,),
        )
        conn.commit()
        return {"ok": True, "deleted": f"#{args.get('ref') or clip_id}"}
    finally:
        conn.close()
