"""Canvas v2.1 — project/sandbox graph storage (Phase 1: entities only).

Scope model (addendum §1): one durable canvas per project (the story bible,
shared across its sessions); one sandbox canvas per unlinked agent session.
Entity auto-seed is idempotent on (entity_type, entity_id) and never
resurrects nodes the user deleted.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from calliope.config import settings
from calliope.db import get_db, row_to_dict
from calliope.events.bus import event_bus

router = APIRouter()

ENTITY_COLUMNS = {
    "character": 80,
    "location": 420,
    "item": 760,
    "scene": 1100,
}

_ENTITY_QUERIES = [
    (
        "character",
        "SELECT id, name AS label FROM characters WHERE project_id = ? ORDER BY id",
    ),
    (
        "location",
        "SELECT id, name AS label FROM locations WHERE project_id = ? ORDER BY id",
    ),
    (
        "item",
        "SELECT id, name AS label FROM items WHERE project_id = ? ORDER BY id",
    ),
    (
        "scene",
        "SELECT id, heading AS label FROM scenes WHERE project_id = ? ORDER BY order_index, id",
    ),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CanvasCreate(BaseModel):
    project_id: int | None = None
    agent_session_id: int | None = None
    title: str = "Untitled Canvas"

    @field_validator("title")
    @classmethod
    def _bound_title(cls, v: str) -> str:
        if len(v) > 300:
            raise ValueError("Title too long (max 300 characters)")
        return v


class CanvasPatch(BaseModel):
    title: str | None = None
    viewport_json: str | None = None

    @field_validator("title")
    @classmethod
    def _bound_title(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 300:
            raise ValueError("Title too long (max 300 characters)")
        return v


class NodePatch(BaseModel):
    x: float | None = None
    y: float | None = None
    title: str | None = None


def _canvas_out(conn, row) -> dict[str, Any]:
    out = row_to_dict(row)
    if out.get("project_id"):
        proj = conn.execute(
            "SELECT id, title FROM projects WHERE id = ?", (out["project_id"],)
        ).fetchone()
        out["project"] = {"id": proj["id"], "title": proj["title"]} if proj else None
    else:
        out["project"] = None
    return out


def _load_graph(conn, canvas_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM canvas WHERE id = ?", (canvas_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Canvas not found")
    nodes = [
        row_to_dict(r)
        for r in conn.execute(
            "SELECT * FROM canvas_node WHERE canvas_id = ? AND deleted = 0 ORDER BY id",
            (canvas_id,),
        ).fetchall()
    ]
    return {"canvas": _canvas_out(conn, row), "nodes": nodes}


def seed_entities(conn, canvas_id: int, project_id: int) -> int:
    """Insert entity nodes for characters/locations/items/scenes not yet on
    the canvas. Idempotent on (entity_type, entity_id). Returns insert count."""
    existing = {
        (r["entity_type"], r["entity_id"])
        for r in conn.execute(
            "SELECT entity_type, entity_id FROM canvas_node WHERE canvas_id = ?",
            (canvas_id,),
        ).fetchall()
    }
    # Tombstoned entity nodes (user deleted the card) must not resurrect.
    existing |= {
        (r["entity_type"], r["entity_id"])
        for r in conn.execute(
            """
            SELECT entity_type, entity_id FROM canvas_node
            WHERE canvas_id = ? AND deleted = 1
            """,
            (canvas_id,),
        ).fetchall()
    }
    inserted = 0
    for entity_type, query in _ENTITY_QUERIES:
        rows = conn.execute(query, (project_id,)).fetchall()
        y = 80
        for r in rows:
            key = (entity_type, r["id"])
            if key in existing:
                continue
            conn.execute(
                """
                INSERT INTO canvas_node
                    (canvas_id, type, title, x, y, entity_type, entity_id,
                     created_at, updated_at)
                VALUES (?, 'entity', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canvas_id,
                    r["label"],
                    ENTITY_COLUMNS[entity_type],
                    y,
                    entity_type,
                    r["id"],
                    _now(),
                    _now(),
                ),
            )
            existing.add(key)
            inserted += 1
            y += 120
    return inserted


@router.get("")
async def list_canvas() -> list[dict[str, Any]]:
    conn = get_db(settings.db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, title, project_id, agent_session_id, updated_at
            FROM canvas ORDER BY updated_at DESC
            """
        ).fetchall()
        return [row_to_dict(r) for r in rows]
    finally:
        conn.close()


@router.post("")
async def create_canvas(payload: CanvasCreate) -> dict[str, Any]:
    if (payload.project_id is None) == (payload.agent_session_id is None):
        raise HTTPException(
            status_code=422, detail="Exactly one of project_id or agent_session_id is required"
        )
    conn = get_db(settings.db_path)
    try:
        canvas_id: int
        if payload.project_id is not None:
            pid = payload.project_id
            if not conn.execute("SELECT id FROM projects WHERE id = ?", (pid,)).fetchone():
                raise HTTPException(status_code=404, detail="Project not found")
            row = conn.execute(
                "SELECT id FROM canvas WHERE project_id = ? ORDER BY id LIMIT 1", (pid,)
            ).fetchone()
            if row:
                canvas_id = int(row["id"])
            else:
                cur = conn.execute(
                    """
                    INSERT INTO canvas (project_id, title, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (pid, payload.title, _now(), _now()),
                )
                canvas_id = int(cur.lastrowid)
            seed_entities(conn, canvas_id, pid)
        else:
            sid = payload.agent_session_id
            session = conn.execute(
                "SELECT id, project_id FROM agent_sessions WHERE id = ?", (sid,)
            ).fetchone()
            if not session:
                raise HTTPException(status_code=404, detail="Session not found")
            if session["project_id"] is not None:
                raise HTTPException(
                    status_code=422,
                    detail="Session is project-linked; create the project canvas instead",
                )
            row = conn.execute(
                "SELECT id FROM canvas WHERE agent_session_id = ? ORDER BY id LIMIT 1", (sid,)
            ).fetchone()
            if row:
                canvas_id = int(row["id"])
            else:
                cur = conn.execute(
                    """
                    INSERT INTO canvas (agent_session_id, title, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (sid, payload.title, _now(), _now()),
                )
                canvas_id = int(cur.lastrowid)

        conn.commit()
        graph = _load_graph(conn, canvas_id)
    finally:
        conn.close()
    await event_bus.publish(
        "canvas.updated",
        {"canvas_id": canvas_id, "reason": "created"},
    )
    return graph


@router.get("/{canvas_id}")
async def get_canvas(canvas_id: int) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        return _load_graph(conn, canvas_id)
    finally:
        conn.close()


@router.patch("/{canvas_id}")
async def patch_canvas(canvas_id: int, payload: CanvasPatch) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        if not conn.execute("SELECT id FROM canvas WHERE id = ?", (canvas_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Canvas not found")
        if payload.title is not None:
            conn.execute(
                "UPDATE canvas SET title = ?, updated_at = ? WHERE id = ?",
                (payload.title, _now(), canvas_id),
            )
        if payload.viewport_json is not None:
            try:
                json.loads(payload.viewport_json)
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=422, detail="viewport_json must be valid JSON"
                ) from exc
            conn.execute(
                "UPDATE canvas SET viewport_json = ?, updated_at = ? WHERE id = ?",
                (payload.viewport_json, _now(), canvas_id),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM canvas WHERE id = ?", (canvas_id,)).fetchone()
        out = _canvas_out(conn, row)
    finally:
        conn.close()
    await event_bus.publish(
        "canvas.updated",
        {"canvas_id": canvas_id, "reason": "patched"},
    )
    return out


@router.delete("/{canvas_id}")
async def delete_canvas(canvas_id: int) -> dict[str, bool]:
    conn = get_db(settings.db_path)
    try:
        if not conn.execute("SELECT id FROM canvas WHERE id = ?", (canvas_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Canvas not found")
        conn.execute("DELETE FROM canvas WHERE id = ?", (canvas_id,))
        conn.commit()
    finally:
        conn.close()
    await event_bus.publish(
        "canvas.updated",
        {"canvas_id": canvas_id, "reason": "deleted"},
    )
    return {"ok": True}


@router.patch("/{canvas_id}/nodes/{node_id}")
async def patch_node(canvas_id: int, node_id: int, payload: NodePatch) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT id FROM canvas_node WHERE id = ? AND canvas_id = ?",
            (node_id, canvas_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Node not found")
        if payload.x is not None:
            conn.execute(
                "UPDATE canvas_node SET x = ?, updated_at = ? WHERE id = ?",
                (payload.x, _now(), node_id),
            )
        if payload.y is not None:
            conn.execute(
                "UPDATE canvas_node SET y = ?, updated_at = ? WHERE id = ?",
                (payload.y, _now(), node_id),
            )
        if payload.title is not None:
            conn.execute(
                "UPDATE canvas_node SET title = ?, updated_at = ? WHERE id = ?",
                (payload.title, _now(), node_id),
            )
        conn.commit()
        out = row_to_dict(
            conn.execute("SELECT * FROM canvas_node WHERE id = ?", (node_id,)).fetchone()
        )
    finally:
        conn.close()
    return out


@router.delete("/{canvas_id}/nodes/{node_id}")
async def delete_node(canvas_id: int, node_id: int) -> dict[str, bool]:
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT id, entity_type, entity_id FROM canvas_node WHERE id = ? AND canvas_id = ?",
            (node_id, canvas_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Node not found")
        if row["entity_type"] is not None and row["entity_id"] is not None:
            # Entity nodes tombstone — hard delete would let auto-seed resurrect
            # the card the user deliberately removed.
            conn.execute(
                "UPDATE canvas_node SET deleted = 1, updated_at = ? WHERE id = ?",
                (_now(), node_id),
            )
        else:
            conn.execute("DELETE FROM canvas_node WHERE id = ?", (node_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}
