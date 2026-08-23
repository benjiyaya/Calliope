"""Story plugin: beats / characters / locations / items generation + CRUD."""
from __future__ import annotations

from functools import partial
from typing import Any

from calliope.agent.harness.registry import ToolContext, ToolDefinition, ToolRegistry, _db
from calliope.agent.prompts import (
    character_sheet_prompt,
    item_reference_prompt,
    location_reference_prompt,
)
from calliope.db import row_to_dict


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="generate_story",
            description=(
                "Generate the story for the linked project: beats, characters, "
                "locations. DESTRUCTIVE: replace=true (default) DELETES existing "
                "beats, characters AND locations first. If the project already "
                "has a story and the user only wants changes, prefer "
                "replace=false or confirm before replacing."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "replace": {
                        "type": "boolean",
                        "description": "Replace existing beats/characters/locations (default true = destructive)",
                    }
                },
            },
            executor=t_generate_story,
            category="story",
            destructive=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="get_story",
            description=(
                "Read the linked project's story: project row, beats, characters, "
                "locations, items with real ids — the source of truth for character_id / "
                "location_id / item_id arguments."
            ),
            parameters={"type": "object", "properties": {}},
            executor=t_get_story,
            category="story",
        )
    )
    _register_asset_crud(registry)


def _register_asset_crud(registry: ToolRegistry) -> None:
    """Register add/update/delete tools for characters, locations and items.

    These mirror the scene CRUD tools (add_scene/update_scene/delete_scene) so
    the agent can make targeted additions without regenerating the whole story.
    """
    registry.register(
        ToolDefinition(
            name="add_character",
            description=(
                "Add a new character to the linked project. name is required; "
                "appearance/personality seed a character-sheet image prompt unless "
                "consistency_prompt is given."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "age": {"type": "string"},
                    "appearance": {"type": "string"},
                    "personality": {"type": "string"},
                    "consistency_prompt": {"type": "string"},
                },
                "required": ["name"],
            },
            executor=partial(t_add_asset, "character"),
            category="story",
        )
    )
    registry.register(
        ToolDefinition(
            name="update_character",
            description=(
                "Update one existing character. character_id must come from "
                "get_story/get_workspace — never guess it. Pass only the fields to change."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "character_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "age": {"type": "string"},
                    "appearance": {"type": "string"},
                    "personality": {"type": "string"},
                    "consistency_prompt": {"type": "string"},
                },
                "required": ["character_id"],
            },
            executor=partial(t_update_asset, "character"),
            category="story",
        )
    )
    registry.register(
        ToolDefinition(
            name="delete_character",
            description=(
                "Delete a character permanently. character_id must come from "
                "get_story/get_workspace. Prefer update_character for content changes."
            ),
            parameters={
                "type": "object",
                "properties": {"character_id": {"type": "integer"}},
                "required": ["character_id"],
            },
            executor=partial(t_delete_asset, "character"),
            category="story",
            destructive=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="add_location",
            description=(
                "Add a new environment/location to the linked project. name is "
                "required; description seeds an environment-reference image prompt "
                "unless consistency_prompt is given."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "consistency_prompt": {"type": "string"},
                },
                "required": ["name"],
            },
            executor=partial(t_add_asset, "location"),
            category="story",
        )
    )
    registry.register(
        ToolDefinition(
            name="update_location",
            description=(
                "Update one existing environment/location. location_id must come "
                "from get_story/get_workspace — never guess it. Pass only the fields to change."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "location_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "consistency_prompt": {"type": "string"},
                },
                "required": ["location_id"],
            },
            executor=partial(t_update_asset, "location"),
            category="story",
        )
    )
    registry.register(
        ToolDefinition(
            name="delete_location",
            description=(
                "Delete an environment/location permanently. location_id must come "
                "from get_story/get_workspace. Prefer update_location for content changes."
            ),
            parameters={
                "type": "object",
                "properties": {"location_id": {"type": "integer"}},
                "required": ["location_id"],
            },
            executor=partial(t_delete_asset, "location"),
            category="story",
            destructive=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="add_item",
            description=(
                "Add a new misc item (prop, weapon, gift, object) to the linked "
                "project. name is required; description seeds an item-reference image "
                "prompt unless consistency_prompt is given."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "consistency_prompt": {"type": "string"},
                },
                "required": ["name"],
            },
            executor=partial(t_add_asset, "item"),
            category="story",
        )
    )
    registry.register(
        ToolDefinition(
            name="update_item",
            description=(
                "Update one existing misc item. item_id must come from "
                "get_story/get_workspace — never guess it. Pass only the fields to change."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "item_id": {"type": "integer"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "consistency_prompt": {"type": "string"},
                },
                "required": ["item_id"],
            },
            executor=partial(t_update_asset, "item"),
            category="story",
        )
    )
    registry.register(
        ToolDefinition(
            name="delete_item",
            description=(
                "Delete a misc item permanently. item_id must come from "
                "get_story/get_workspace. Prefer update_item for content changes."
            ),
            parameters={
                "type": "object",
                "properties": {"item_id": {"type": "integer"}},
                "required": ["item_id"],
            },
            executor=partial(t_delete_asset, "item"),
            category="story",
            destructive=True,
        )
    )


async def t_generate_story(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from calliope.routers.story import generate_story as _generate_story

    replace = args.get("replace", True)
    return await _generate_story(ctx.project_id, replace=bool(replace))


async def t_get_story(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from calliope.routers.story import get_story as _get_story

    return await _get_story(ctx.project_id)


# ── asset CRUD executors ─────────────────────────────────────────


# Per-entity metadata: table, id arg key, writable columns, and the prompt
# template used to seed consistency_prompt on create (mirrors story.py routers).
_ENTITY_META: dict[str, dict[str, Any]] = {
    "character": {
        "label": "Character",
        "table": "characters",
        "id_key": "character_id",
        "columns": ("name", "role", "age", "appearance", "personality", "consistency_prompt"),
        "seed": character_sheet_prompt,
    },
    "location": {
        "label": "Location",
        "table": "locations",
        "id_key": "location_id",
        "columns": ("name", "description", "consistency_prompt"),
        "seed": location_reference_prompt,
    },
    "item": {
        "label": "Item",
        "table": "items",
        "id_key": "item_id",
        "columns": ("name", "description", "consistency_prompt"),
        "seed": item_reference_prompt,
    },
}


async def t_add_asset(entity: str, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    meta = _ENTITY_META[entity]
    name = (args.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": f"{meta['label']} name is required"}
    cols = meta["columns"]
    data = {k: args.get(k) for k in cols}
    data["name"] = name
    if not (data.get("consistency_prompt") or "").strip():
        data["consistency_prompt"] = meta["seed"](data)
    conn = _db()
    try:
        fields = ", ".join(cols)
        placeholders = ", ".join(f":{k}" for k in cols)
        cur = conn.execute(
            f"INSERT INTO {meta['table']} (project_id, {fields}) "
            f"VALUES (:project_id, {placeholders})",
            {"project_id": ctx.project_id, **data},
        )
        conn.execute(
            "UPDATE projects SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (ctx.project_id,),
        )
        conn.commit()
        row = conn.execute(
            f"SELECT * FROM {meta['table']} WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return {"ok": True, "created": row_to_dict(row)}
    finally:
        conn.close()


async def t_update_asset(entity: str, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    meta = _ENTITY_META[entity]
    eid = int(args[meta["id_key"]])
    data = {k: v for k, v in args.items() if k in meta["columns"] and v is not None}
    # name is NOT NULL — never let a blank name through an update.
    if "name" in data and not str(data["name"]).strip():
        data.pop("name")
    conn = _db()
    try:
        existing = conn.execute(
            f"SELECT id FROM {meta['table']} WHERE id = ? AND project_id = ?",
            (eid, ctx.project_id),
        ).fetchone()
        if not existing:
            return {"ok": False, "error": f"{meta['label']} {eid} not found in this project"}
        if data:
            fields = ", ".join(f"{k} = :{k}" for k in data)
            conn.execute(
                f"UPDATE {meta['table']} SET {fields} WHERE id = :id AND project_id = :project_id",
                {**data, "id": eid, "project_id": ctx.project_id},
            )
        conn.execute(
            "UPDATE projects SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (ctx.project_id,),
        )
        conn.commit()
        row = conn.execute(
            f"SELECT * FROM {meta['table']} WHERE id = ?", (eid,)
        ).fetchone()
        return {"ok": True, "updated": row_to_dict(row)}
    finally:
        conn.close()


async def t_delete_asset(entity: str, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    meta = _ENTITY_META[entity]
    eid = int(args[meta["id_key"]])
    conn = _db()
    try:
        cur = conn.execute(
            f"DELETE FROM {meta['table']} WHERE id = ? AND project_id = ?",
            (eid, ctx.project_id),
        )
        conn.execute(
            "UPDATE projects SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (ctx.project_id,),
        )
        conn.commit()
        if cur.rowcount == 0:
            return {"ok": False, "error": f"{meta['label']} {eid} not found in this project"}
        return {"ok": True, "deleted_id": eid}
    finally:
        conn.close()
