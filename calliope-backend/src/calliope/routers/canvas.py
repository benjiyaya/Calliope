"""Canvas v2.1 — project/sandbox graph storage (Phase 1: entities only).

Scope model (addendum §1): one durable canvas per project (the story bible,
shared across its sessions); one sandbox canvas per unlinked agent session.
Entity auto-seed is idempotent on (entity_type, entity_id) and never
resurrects nodes the user deleted.
"""
from __future__ import annotations

import json
import sqlite3
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
    "location": 400,
    "item": 720,
    "scene": 1280,
}
# Entity cards are ~190px tall (header + 132px media). 120px rows made them
# stack on top of each other (the "nodes stacking on one another" bug).
ENTITY_ROW_HEIGHT = 280
# Artifacts live in a GRID to the right of the scene column, not a single
# vertical strip at x=1000 (which collided with item/scene columns and grew
# 9000px tall with 30 outputs).
ARTIFACT_GRID_X = 1640
ARTIFACT_COL_WIDTH = 260
ARTIFACT_GRID_COLS = 3
ARTIFACT_ROW_HEIGHT = 240
# Legacy single-column constant kept for back-compat reads; new placements
# use the grid above.
ARTIFACT_COLUMN_X = ARTIFACT_GRID_X

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
    # None → derived from the project/session title (trimmed); explicit values win.
    title: str | None = None

    @field_validator("title")
    @classmethod
    def _bound_title(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 300:
            raise ValueError("Title too long (max 300 characters)")
        return v


def _derive_canvas_title(conn: sqlite3.Connection, payload: CanvasCreate) -> str:
    """Name a new canvas after what it shows: the project, or the session."""
    title = (payload.title or "").strip()
    if title:
        return title
    row = None
    if payload.project_id is not None:
        row = conn.execute(
            "SELECT title FROM projects WHERE id = ?", (payload.project_id,)
        ).fetchone()
    elif payload.agent_session_id is not None:
        row = conn.execute(
            "SELECT title FROM agent_sessions WHERE id = ?", (payload.agent_session_id,)
        ).fetchone()
    name = (row["title"] if row else "").strip()
    return name[:60] or "Canvas"


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
    input_values_json: str | None = None
    status: str | None = None
    artifact_path: str | None = None


class NodeCreate(BaseModel):
    type: str  # 'workflow' | artifact ('image'/'video'); entity nodes auto-seed
    title: str | None = None
    # Omit x/y to let the backend place the node (artifact gallery slot).
    x: float | None = None
    y: float | None = None
    width: float | None = None
    height: float | None = None
    workflow_id: int | None = None
    artifact_path: str | None = None
    job_id: int | None = None
    status: str = "idle"
    input_values_json: str = "{}"

    @field_validator("type")
    @classmethod
    def _type_allowed(cls, v: str) -> str:
        # 'image'/'video' = artifact nodes (generated media); entity nodes are
        # auto-seeded only. Matches the canvas_node CHECK constraint.
        if v not in ("workflow", "image", "video"):
            raise ValueError("type must be 'workflow', 'image', or 'video'")
        return v

    @field_validator("title")
    @classmethod
    def _bound_title(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 300:
            raise ValueError("Title too long (max 300 characters)")
        return v

    @field_validator("input_values_json")
    @classmethod
    def _iv_is_json(cls, v: str) -> str:
        try:
            json.loads(v)
        except json.JSONDecodeError as exc:
            raise ValueError("input_values_json must be valid JSON") from exc
        return v


class EdgeCreate(BaseModel):
    src_node_id: int
    dst_node_id: int
    kind: str  # 'data' | 'link'
    label: str | None = None
    dst_role: str | None = None
    dst_comfy_node_id: str | None = None

    @field_validator("kind")
    @classmethod
    def _kind_allowed(cls, v: str) -> str:
        if v not in ("data", "link"):
            raise ValueError("kind must be 'data' or 'link'")
        return v


class EdgePatch(BaseModel):
    label: str | None = None


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
    edges = [
        row_to_dict(r)
        for r in conn.execute(
            """
            SELECT * FROM canvas_edge WHERE canvas_id = ?
              AND src_node_id IN (SELECT id FROM canvas_node WHERE canvas_id = ? AND deleted = 0)
              AND dst_node_id IN (SELECT id FROM canvas_node WHERE canvas_id = ? AND deleted = 0)
            ORDER BY id
            """,
            (canvas_id, canvas_id, canvas_id),
        ).fetchall()
    ]
    return {"canvas": _canvas_out(conn, row), "nodes": nodes, "edges": edges}


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
        # Start BELOW the lowest live entity card in this column so new
        # entities never land on top of existing ones (the stacking bug).
        col_x = ENTITY_COLUMNS[entity_type]
        lowest = conn.execute(
            """
            SELECT MAX(y) AS max_y FROM canvas_node
            WHERE canvas_id = ? AND deleted = 0 AND type = 'entity'
              AND entity_type = ?
            """,
            (canvas_id, entity_type),
        ).fetchone()
        y = (
            float(lowest["max_y"]) + ENTITY_ROW_HEIGHT
            if lowest and lowest["max_y"] is not None
            else 80.0
        )
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
                    col_x,
                    y,
                    entity_type,
                    r["id"],
                    _now(),
                    _now(),
                ),
            )
            existing.add(key)
            inserted += 1
            y += ENTITY_ROW_HEIGHT
    return inserted


def next_artifact_slot(conn, canvas_id: int) -> tuple[float, float]:
    """Next free cell in the artifact GRID (row-major, 3 columns).

    Scans grid cells in order and returns the first one with no live
    artifact node near it (within half a cell), so user-moved cards keep
    their spot and new outputs fill the gaps instead of stacking.
    """
    rows = conn.execute(
        """
        SELECT x, y FROM canvas_node
        WHERE canvas_id = ? AND deleted = 0 AND type IN ('image', 'video')
        """,
        (canvas_id,),
    ).fetchall()
    taken: list[tuple[float, float]] = [(r["x"], r["y"]) for r in rows]
    tol_x = ARTIFACT_COL_WIDTH / 2
    tol_y = ARTIFACT_ROW_HEIGHT / 2
    index = 0
    while True:
        col = index % ARTIFACT_GRID_COLS
        row = index // ARTIFACT_GRID_COLS
        x = ARTIFACT_GRID_X + col * ARTIFACT_COL_WIDTH
        y = 80.0 + row * ARTIFACT_ROW_HEIGHT
        if not any(abs(tx - x) < tol_x and abs(ty - y) < tol_y for tx, ty in taken):
            return float(x), float(y)
        index += 1


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
                    (pid, _derive_canvas_title(conn, payload), _now(), _now()),
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
                    (sid, _derive_canvas_title(conn, payload), _now(), _now()),
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
        if payload.input_values_json is not None:
            try:
                json.loads(payload.input_values_json)
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=422, detail="input_values_json must be valid JSON"
                ) from exc
            conn.execute(
                "UPDATE canvas_node SET input_values_json = ?, updated_at = ? WHERE id = ?",
                (payload.input_values_json, _now(), node_id),
            )
        if payload.status is not None:
            conn.execute(
                "UPDATE canvas_node SET status = ?, updated_at = ? WHERE id = ?",
                (payload.status, _now(), node_id),
            )
        if payload.artifact_path is not None:
            conn.execute(
                "UPDATE canvas_node SET artifact_path = ?, updated_at = ? WHERE id = ?",
                (payload.artifact_path, _now(), node_id),
            )
        conn.commit()
        out = row_to_dict(
            conn.execute("SELECT * FROM canvas_node WHERE id = ?", (node_id,)).fetchone()
        )
    finally:
        conn.close()
    await event_bus.publish("canvas.updated", {"canvas_id": canvas_id, "reason": "node_patched"})
    return out


@router.post("/{canvas_id}/tidy")
async def tidy_canvas(canvas_id: int) -> dict[str, Any]:
    """Re-layout every node into clean columns/grids (repairs canvases whose
    nodes drifted or stacked). Entity types get one column each, stacked
    top-down; artifacts fill a 3-col grid to the right; workflow nodes sit
    between scenes and the artifact grid. User-moved positions are reset —
    that is the point of Tidy."""
    conn = get_db(settings.db_path)
    try:
        if not conn.execute("SELECT id FROM canvas WHERE id = ?", (canvas_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Canvas not found")
        nodes = conn.execute(
            "SELECT id, type, entity_type FROM canvas_node "
            "WHERE canvas_id = ? AND deleted = 0 ORDER BY id",
            (canvas_id,),
        ).fetchall()
        moved = 0
        # Entity columns: stack each type top-down in its own column.
        col_y: dict[str, float] = {}
        for n in nodes:
            if n["type"] != "entity":
                continue
            et = n["entity_type"] or "character"
            x = ENTITY_COLUMNS.get(et, 80)
            y = col_y.get(et, 80.0)
            conn.execute(
                "UPDATE canvas_node SET x = ?, y = ?, updated_at = ? WHERE id = ?",
                (x, y, _now(), n["id"]),
            )
            col_y[et] = y + ENTITY_ROW_HEIGHT
            moved += 1
        # Workflow nodes: a column between scenes and the artifact grid.
        wf_y = 80.0
        for n in nodes:
            if n["type"] != "workflow":
                continue
            conn.execute(
                "UPDATE canvas_node SET x = ?, y = ?, updated_at = ? WHERE id = ?",
                (1480, wf_y, _now(), n["id"]),
            )
            wf_y += 200.0
            moved += 1
        # Artifacts: 3-col grid to the right.
        idx = 0
        for n in nodes:
            if n["type"] not in ("image", "video"):
                continue
            col = idx % ARTIFACT_GRID_COLS
            row = idx // ARTIFACT_GRID_COLS
            x = ARTIFACT_GRID_X + col * ARTIFACT_COL_WIDTH
            y = 80.0 + row * ARTIFACT_ROW_HEIGHT
            conn.execute(
                "UPDATE canvas_node SET x = ?, y = ?, updated_at = ? WHERE id = ?",
                (x, y, _now(), n["id"]),
            )
            idx += 1
            moved += 1
        conn.commit()
    finally:
        conn.close()
    await event_bus.publish("canvas.updated", {"canvas_id": canvas_id, "reason": "tidied"})
    return {"ok": True, "moved": moved}


@router.post("/{canvas_id}/nodes")
async def create_node(canvas_id: int, payload: NodeCreate) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        if not conn.execute("SELECT id FROM canvas WHERE id = ?", (canvas_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Canvas not found")
        if payload.type == "workflow":
            if payload.workflow_id is None:
                raise HTTPException(
                    status_code=422, detail="workflow nodes require workflow_id"
                )
            wf = conn.execute(
                "SELECT id FROM workflows WHERE id = ?", (payload.workflow_id,)
            ).fetchone()
            if not wf:
                raise HTTPException(status_code=404, detail="Workflow not found")
        if payload.artifact_path is not None:
            # Register the path under the canvas's owning scope so folder-move
            # rebasing catches it (entity/artifact paths may outlive the canvas).
            _register_artifact_path(conn, canvas_id, payload.artifact_path)
        x, y = payload.x, payload.y
        if x is None or y is None:
            # No position given: place via the shared artifact-slot layout so
            # every creator (UI SSE, agent tool) stacks cards consistently.
            x, y = next_artifact_slot(conn, canvas_id)
        try:
            cur = conn.execute(
                """
                INSERT INTO canvas_node
                    (canvas_id, type, title, x, y, width, height, workflow_id,
                     artifact_path, job_id, status, input_values_json,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canvas_id,
                    payload.type,
                    payload.title,
                    x,
                    y,
                    payload.width,
                    payload.height,
                    payload.workflow_id,
                    payload.artifact_path,
                    payload.job_id,
                    payload.status,
                    payload.input_values_json,
                    _now(),
                    _now(),
                ),
            )
        except sqlite3.IntegrityError:
            # Unique artifact-per-job index (uq_canvas_node_job): another
            # writer (agent tool / SSE materializer) already posted this
            # job's output. Return the winner instead of a 500 — idempotent.
            conn.rollback()
            existing = conn.execute(
                """
                SELECT * FROM canvas_node
                WHERE canvas_id = ? AND job_id = ? AND deleted = 0
                  AND type IN ('image', 'video')
                LIMIT 1
                """,
                (canvas_id, payload.job_id),
            ).fetchone()
            if existing:
                return row_to_dict(existing)
            raise HTTPException(status_code=409, detail="Duplicate artifact node")
        node_id = int(cur.lastrowid)
        conn.commit()
        out = row_to_dict(
            conn.execute("SELECT * FROM canvas_node WHERE id = ?", (node_id,)).fetchone()
        )
    finally:
        conn.close()
    await event_bus.publish("canvas.updated", {"canvas_id": canvas_id, "reason": "node_created"})
    return out


def _register_artifact_path(conn, canvas_id: int, path: str) -> None:
    """Artifact paths under a project canvas are project assets; sandbox ones
    live in the scratch. Nothing to do today beyond keeping the column — the
    rebase migration reads _PATH_COLUMNS. Hook kept for symmetry."""
    return None


# ---- edges ---------------------------------------------------------------


def _get_node_or_404(conn, canvas_id: int, node_id: int):
    row = conn.execute(
        """
        SELECT id, type, entity_type, artifact_path FROM canvas_node
        WHERE id = ? AND canvas_id = ?
        """,
        (node_id, canvas_id),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Node not found in this canvas")
    return row


def _has_image_source(node) -> bool:
    """Nodes that can feed a data edge: artifacts with a path, or entity
    nodes backed by a reference image (characters, locations, items)."""
    if node["artifact_path"]:
        return True
    if node["type"] == "entity" and node["entity_type"] in ("character", "location", "item"):
        return True
    return False


def _data_edge_creates_cycle(conn, canvas_id: int, src: int, dst: int) -> bool:
    """Would adding src→dst close a cycle in the data-edge subgraph?
    True iff src is reachable from dst walking existing data edges forward —
    src→dst would then complete the loop dst→…→src→dst."""
    seen = {dst}
    frontier = [dst]
    while frontier:
        current = frontier.pop()
        if current == src:
            return True
        rows = conn.execute(
            """
            SELECT dst_node_id FROM canvas_edge
            WHERE canvas_id = ? AND kind = 'data' AND src_node_id = ?
            """,
            (canvas_id, current),
        ).fetchall()
        for r in rows:
            nxt = r["dst_node_id"]
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return False


@router.post("/{canvas_id}/edges")
async def create_edge(canvas_id: int, payload: EdgeCreate) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        if not conn.execute("SELECT id FROM canvas WHERE id = ?", (canvas_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Canvas not found")
        src = _get_node_or_404(conn, canvas_id, payload.src_node_id)
        _get_node_or_404(conn, canvas_id, payload.dst_node_id)
        if payload.src_node_id == payload.dst_node_id and payload.kind == "data":
            raise HTTPException(status_code=422, detail="Self-loops are not allowed on data edges")
        if payload.kind == "data":
            if not payload.dst_role:
                raise HTTPException(
                    status_code=422, detail="data edges require dst_role (the workflow input role)"
                )
            if not _has_image_source(src):
                raise HTTPException(
                    status_code=422,
                    detail="Source node has no image/artifact to feed this input",
                )
            if _data_edge_creates_cycle(conn, canvas_id, payload.src_node_id, payload.dst_node_id):
                raise HTTPException(
                    status_code=422, detail="This data edge would create a cycle"
                )
        else:
            if payload.label is None or not payload.label.strip():
                raise HTTPException(status_code=422, detail="link edges require a label")
            if len(payload.label) > 80:
                raise HTTPException(status_code=422, detail="Label too long (max 80 characters)")
            label = payload.label.strip()
        dup = conn.execute(
            """
            SELECT id FROM canvas_edge
            WHERE canvas_id = ? AND src_node_id = ? AND dst_node_id = ? AND kind = ?
            """,
            (canvas_id, payload.src_node_id, payload.dst_node_id, payload.kind),
        ).fetchone()
        if dup:
            raise HTTPException(status_code=422, detail="This edge already exists")
        cur = conn.execute(
            """
            INSERT INTO canvas_edge
                (canvas_id, src_node_id, dst_node_id, kind, label, dst_role,
                 dst_comfy_node_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                canvas_id,
                payload.src_node_id,
                payload.dst_node_id,
                payload.kind,
                label if payload.kind == "link" else None,
                payload.dst_role,
                payload.dst_comfy_node_id,
                _now(),
            ),
        )
        edge_id = int(cur.lastrowid)
        conn.commit()
        out = row_to_dict(
            conn.execute("SELECT * FROM canvas_edge WHERE id = ?", (edge_id,)).fetchone()
        )
    finally:
        conn.close()
    await event_bus.publish("canvas.updated", {"canvas_id": canvas_id, "reason": "edge_created"})
    return out


@router.patch("/{canvas_id}/edges/{edge_id}")
async def patch_edge(canvas_id: int, edge_id: int, payload: EdgePatch) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT id, kind FROM canvas_edge WHERE id = ? AND canvas_id = ?",
            (edge_id, canvas_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Edge not found")
        if row["kind"] != "link":
            raise HTTPException(status_code=422, detail="Only link edges have editable labels")
        if payload.label is None or not payload.label.strip():
            raise HTTPException(status_code=422, detail="Label required")
        if len(payload.label) > 80:
            raise HTTPException(status_code=422, detail="Label too long (max 80 characters)")
        conn.execute(
            "UPDATE canvas_edge SET label = ? WHERE id = ?",
            (payload.label.strip(), edge_id),
        )
        conn.commit()
        out = row_to_dict(
            conn.execute("SELECT * FROM canvas_edge WHERE id = ?", (edge_id,)).fetchone()
        )
    finally:
        conn.close()
    await event_bus.publish("canvas.updated", {"canvas_id": canvas_id, "reason": "edge_patched"})
    return out


@router.delete("/{canvas_id}/edges/{edge_id}")
async def delete_edge(canvas_id: int, edge_id: int) -> dict[str, bool]:
    conn = get_db(settings.db_path)
    try:
        if not conn.execute(
            "SELECT id FROM canvas_edge WHERE id = ? AND canvas_id = ?",
            (edge_id, canvas_id),
        ).fetchone():
            raise HTTPException(status_code=404, detail="Edge not found")
        conn.execute("DELETE FROM canvas_edge WHERE id = ?", (edge_id,))
        conn.commit()
    finally:
        conn.close()
    await event_bus.publish("canvas.updated", {"canvas_id": canvas_id, "reason": "edge_deleted"})
    return {"ok": True}


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
