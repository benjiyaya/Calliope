"""Characters, locations, and items."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from calliope.agent.capabilities.sql import as_int, connect, touch
from calliope.config import settings
from calliope.db import row_to_dict

_TABLES = {
    "character": (
        "characters",
        ("name", "role", "age", "appearance", "personality", "consistency_prompt"),
    ),
    "location": ("locations", ("name", "description", "consistency_prompt")),
    "item": ("items", ("name", "description", "consistency_prompt")),
}


def list_entities(project_id: int, kind: str) -> dict[str, Any]:
    table, columns = _TABLES[kind]
    conn = connect()
    try:
        rows = conn.execute(
            f"SELECT id, {', '.join(columns)} FROM {table} WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()
        return {"ok": True, kind + "s": [row_to_dict(row) for row in rows]}
    finally:
        conn.close()


def add_entity(project_id: int, kind: str, args: dict[str, Any]) -> dict[str, Any]:
    table, columns = _TABLES[kind]
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        return {"ok": False, "error": "name is required"}
    values = [args.get(column) for column in columns]
    values[0] = name.strip()
    conn = connect()
    try:
        placeholders = ", ".join("?" for _ in columns)
        cur = conn.execute(
            f"INSERT INTO {table} (project_id, {', '.join(columns)}) "
            f"VALUES (?, {placeholders})",
            (project_id, *values),
        )
        touch(conn, project_id)
        conn.commit()
        row = conn.execute(
            f"SELECT id, {', '.join(columns)} FROM {table} WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        return {"ok": True, kind: row_to_dict(row)}
    finally:
        conn.close()


def update_entity(project_id: int, kind: str, args: dict[str, Any]) -> dict[str, Any]:
    table, columns = _TABLES[kind]
    entity_id = as_int(args.get(f"{kind}_id"))
    if entity_id is None:
        return {"ok": False, "error": f"{kind}_id is required"}
    data = {key: args[key] for key in columns if args.get(key) is not None}
    conn = connect()
    try:
        existing = conn.execute(
            f"SELECT id FROM {table} WHERE id = ? AND project_id = ?",
            (entity_id, project_id),
        ).fetchone()
        if not existing:
            return {"ok": False, "error": f"{kind} {entity_id} not found in this project"}
        if data:
            fields = ", ".join(f"{key} = :{key}" for key in data)
            conn.execute(
                f"UPDATE {table} SET {fields} WHERE id = :id AND project_id = :project_id",
                {**data, "id": entity_id, "project_id": project_id},
            )
            touch(conn, project_id)
            conn.commit()
        row = conn.execute(
            f"SELECT id, {', '.join(columns)} FROM {table} WHERE id = ?",
            (entity_id,),
        ).fetchone()
        return {"ok": True, kind: row_to_dict(row)}
    finally:
        conn.close()


def delete_entity(project_id: int, kind: str, args: dict[str, Any]) -> dict[str, Any]:
    table, _columns = _TABLES[kind]
    entity_id = as_int(args.get(f"{kind}_id"))
    if entity_id is None:
        return {"ok": False, "error": f"{kind}_id is required"}
    conn = connect()
    try:
        cur = conn.execute(
            f"DELETE FROM {table} WHERE id = ? AND project_id = ?",
            (entity_id, project_id),
        )
        touch(conn, project_id)
        conn.commit()
        if cur.rowcount == 0:
            return {"ok": False, "error": f"{kind} {entity_id} not found in this project"}
        return {"ok": True, "deleted_id": entity_id}
    finally:
        conn.close()


def _asset_file(path: str) -> Path | None:
    try:
        target = Path(path).expanduser().resolve()
        target.relative_to(Path(settings.assets_dir).resolve())
    except (OSError, ValueError):
        return None
    if not target.is_file():
        return None
    return target


def attach_image(project_id: int, args: dict[str, Any]) -> dict[str, Any]:
    """File an existing image onto a character, location, or item. Does not queue."""
    kind = str(args.get("entity") or args.get("kind") or "").strip()
    if kind not in _TABLES:
        return {"ok": False, "error": "entity must be character, location, or item"}
    entity_id = as_int(args.get("id") or args.get(f"{kind}_id"))
    if entity_id is None:
        return {"ok": False, "error": "id is required"}
    raw = str(args.get("path") or "").strip()
    if not raw:
        return {"ok": False, "error": "path is required"}
    target = _asset_file(raw)
    if target is None:
        return {"ok": False, "error": "path must be a file under the assets folder"}
    if kind == "character":
        slot = str(args.get("target") or "sheet").strip()
        if slot not in ("sheet", "portrait"):
            return {"ok": False, "error": "target must be sheet or portrait"}
        column = "sheet_path" if slot == "sheet" else "portrait_path"
    else:
        column = "reference_image_path"
    table, _columns = _TABLES[kind]
    conn = connect()
    try:
        existing = conn.execute(
            f"SELECT id FROM {table} WHERE id = ? AND project_id = ?",
            (entity_id, project_id),
        ).fetchone()
        if not existing:
            return {"ok": False, "error": f"{kind} {entity_id} not found in this project"}
        conn.execute(
            f"UPDATE {table} SET {column} = ? WHERE id = ? AND project_id = ?",
            (str(target), entity_id, project_id),
        )
        touch(conn, project_id)
        conn.commit()
        return {"ok": True, "entity": kind, "id": entity_id, "path": str(target)}
    finally:
        conn.close()
