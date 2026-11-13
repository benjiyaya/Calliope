"""Canvas cards for the session or project board."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from calliope.agent.capabilities.sql import as_int, connect
from calliope.config import settings
from calliope.db import row_to_dict


def canvas_for(project_id: int | None, session_id: int | None) -> dict[str, Any]:
    conn = connect()
    try:
        if project_id is not None:
            row = conn.execute(
                "SELECT * FROM canvas WHERE project_id = ? ORDER BY id LIMIT 1",
                (project_id,),
            ).fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO canvas (project_id, title) VALUES (?, 'Project canvas')",
                    (project_id,),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM canvas WHERE id = ?", (cur.lastrowid,)).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM canvas
                WHERE agent_session_id = ? AND project_id IS NULL
                ORDER BY id LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO canvas (agent_session_id, title) VALUES (?, 'Sandbox canvas')",
                    (session_id,),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM canvas WHERE id = ?", (cur.lastrowid,)).fetchone()
        return row_to_dict(row)
    finally:
        conn.close()


def summarize_canvas(project_id: int | None, session_id: int | None) -> dict[str, Any]:
    canvas = canvas_for(project_id, session_id)
    conn = connect()
    try:
        nodes = conn.execute(
            """
            SELECT id, type, title, x, y, status, entity_type, entity_id, deleted
            FROM canvas_node WHERE canvas_id = ? AND deleted = 0 ORDER BY id
            """,
            (canvas["id"],),
        ).fetchall()
        edges = conn.execute(
            """
            SELECT id, src_node_id, dst_node_id, kind, label, dst_role
            FROM canvas_edge WHERE canvas_id = ? ORDER BY id
            """,
            (canvas["id"],),
        ).fetchall()
        return {
            "ok": True,
            "canvas_id": canvas["id"],
            "nodes": [row_to_dict(row) for row in nodes],
            "edges": [row_to_dict(row) for row in edges],
        }
    finally:
        conn.close()


def create_canvas_node(
    project_id: int | None, session_id: int | None, args: dict[str, Any]
) -> dict[str, Any]:
    kind = args.get("type") or "text"
    if kind not in ("entity", "image", "video", "text", "workflow"):
        return {"ok": False, "error": "type must be entity, image, video, text, or workflow"}
    title = (args.get("title") or kind).strip()
    if not title:
        return {"ok": False, "error": "title is required"}
    canvas = canvas_for(project_id, session_id)
    conn = connect()
    try:
        cur = conn.execute(
            """
            INSERT INTO canvas_node
                (canvas_id, type, title, x, y, workflow_id, entity_type, entity_id,
                 artifact_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                canvas["id"],
                kind,
                title,
                float(args.get("x") or 80),
                float(args.get("y") or 80),
                as_int(args.get("workflow_id")),
                args.get("entity_type"),
                as_int(args.get("entity_id")),
                args.get("artifact_path"),
            ),
        )
        conn.commit()
        return {"ok": True, "node_id": int(cur.lastrowid), "canvas_id": canvas["id"]}
    finally:
        conn.close()


def update_canvas_node(
    project_id: int | None, session_id: int | None, args: dict[str, Any]
) -> dict[str, Any]:
    node_id = as_int(args.get("node_id"))
    if node_id is None:
        return {"ok": False, "error": "node_id is required"}
    canvas = canvas_for(project_id, session_id)
    data: dict[str, Any] = {}
    for key in ("title", "x", "y", "status"):
        if args.get(key) is not None:
            data[key] = args[key]
    if args.get("input_values") is not None:
        data["input_values_json"] = json.dumps(args["input_values"])
    if not data:
        return {"ok": False, "error": "Nothing to update"}
    conn = connect()
    try:
        fields = ", ".join(f"{key} = :{key}" for key in data)
        cur = conn.execute(
            f"UPDATE canvas_node SET {fields}, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = :id AND canvas_id = :canvas_id",
            {**data, "id": node_id, "canvas_id": canvas["id"]},
        )
        conn.commit()
        if cur.rowcount == 0:
            return {"ok": False, "error": f"Node {node_id} not found on this canvas"}
        return {"ok": True, "node_id": node_id}
    finally:
        conn.close()


def delete_canvas_node(
    project_id: int | None, session_id: int | None, args: dict[str, Any]
) -> dict[str, Any]:
    node_id = as_int(args.get("node_id"))
    if node_id is None:
        return {"ok": False, "error": "node_id is required"}
    canvas = canvas_for(project_id, session_id)
    conn = connect()
    try:
        row = conn.execute(
            "SELECT type FROM canvas_node WHERE id = ? AND canvas_id = ?",
            (node_id, canvas["id"]),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"Node {node_id} not found on this canvas"}
        if row["type"] == "entity":
            conn.execute(
                "UPDATE canvas_node SET deleted = 1 WHERE id = ?",
                (node_id,),
            )
        else:
            conn.execute("DELETE FROM canvas_node WHERE id = ?", (node_id,))
        conn.commit()
        return {"ok": True, "node_id": node_id}
    finally:
        conn.close()


def connect_nodes(
    project_id: int | None, session_id: int | None, args: dict[str, Any], *, kind: str
) -> dict[str, Any]:
    src = as_int(args.get("src_node_id"))
    dst = as_int(args.get("dst_node_id"))
    if src is None or dst is None:
        return {"ok": False, "error": "src_node_id and dst_node_id are required"}
    if kind == "data" and not (args.get("dst_role") or "").strip():
        return {"ok": False, "error": "dst_role is required on a data edge"}
    if kind == "link" and not (args.get("label") or "").strip():
        return {"ok": False, "error": "label is required on a link edge"}
    canvas = canvas_for(project_id, session_id)
    conn = connect()
    try:
        if src == dst:
            return {"ok": False, "error": "an edge cannot loop on one node"}
        if kind == "data":
            # Cycle-check: BFS downstream from dst must never reach src (a
            # data edge feeds generation — a cycle is a wiring mistake).
            # Link edges explicitly allow cycles (story-bible annotations).
            frontier = [dst]
            seen: set[int] = set()
            while frontier:
                current = frontier.pop()
                if current == src:
                    return {"ok": False, "error": "this edge would create a cycle"}
                if current in seen:
                    continue
                seen.add(current)
                frontier.extend(
                    int(row["dst_node_id"])
                    for row in conn.execute(
                        """
                        SELECT dst_node_id FROM canvas_edge
                        WHERE canvas_id = ? AND src_node_id = ? AND kind = 'data'
                        """,
                        (canvas["id"], current),
                    ).fetchall()
                )
            dup = conn.execute(
                """
                SELECT id FROM canvas_edge
                WHERE canvas_id = ? AND src_node_id = ? AND dst_node_id = ? AND kind = ?
                """,
                (canvas["id"], src, dst, kind),
            ).fetchone()
            if dup:
                return {"ok": False, "error": "this edge already exists"}
        cur = conn.execute(
            """
            INSERT INTO canvas_edge
                (canvas_id, src_node_id, dst_node_id, kind, label, dst_role)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                canvas["id"],
                src,
                dst,
                kind,
                args.get("label"),
                args.get("dst_role"),
            ),
        )
        conn.commit()
        return {"ok": True, "edge_id": int(cur.lastrowid)}
    finally:
        conn.close()


def _media(value: Any) -> dict[str, list[str]]:
    found = {"images": [], "audios": [], "videos": []}
    images = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    audios = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac"}
    videos = {".mp4", ".webm", ".mov", ".mkv"}

    def walk(item: Any) -> None:
        if isinstance(item, str):
            suffix = Path(item).suffix.lower()
            bucket = None
            if suffix in images:
                bucket = "images"
            elif suffix in audios:
                bucket = "audios"
            elif suffix in videos:
                bucket = "videos"
            if bucket and item not in found[bucket]:
                found[bucket].append(item)
        elif isinstance(item, dict):
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return found


def _resolve_absolute(path: str) -> str | None:
    """Paths in jobs.output_paths / asset columns are absolute (and rebased on
    folder moves). Accept the stored value or a basename under assets_dir."""
    candidates = [Path(path), Path(settings.assets_dir) / path.replace("\\", "/").lstrip("/\\")]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


async def post_artifact_to_canvas(
    project_id: int | None, session_id: int | None, args: dict[str, Any]
) -> dict[str, Any]:
    """Drop a job output (or an existing asset path) onto the canvas as a card."""
    import re as _re

    from calliope.events.bus import event_bus
    from calliope.routers.canvas import next_artifact_slot

    canvas = canvas_for(project_id, session_id)
    canvas_id = int(canvas["id"])

    job_id = as_int(args.get("job_id"))
    # "asset_path" is the agent-tool name; "artifact_path" the runtime one —
    # accept both, plus an explicit kind override.
    asset_path = str(args.get("asset_path") or args.get("artifact_path") or "").strip()
    kind_override = args.get("kind") if args.get("kind") in ("image", "video") else None
    title = str(args.get("title") or "").strip()

    if job_id is not None:
        conn = connect()
        try:
            job = conn.execute(
                "SELECT id, kind, status, output_paths_json FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        finally:
            conn.close()
        if not job:
            return {"ok": False, "error": f"Job {job_id} not found"}
        try:
            outputs = json.loads(job["output_paths_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            outputs = []
        if not outputs:
            return {
                "ok": False,
                "error": f"Job {job_id} has no outputs yet (status: {job['status']}). "
                "Use wait_for_jobs first.",
            }
        path = str(outputs[0])
        ntype = "video" if str(job["kind"]) == "video" else "image"
        if not title:
            title = f"Job {job['id']} output"
    elif asset_path:
        path = asset_path
        ntype = kind_override or (
            "video" if _re.search(r"\.(mp4|webm|mov|avi)$", path, _re.IGNORECASE) else "image"
        )
        if not title:
            title = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or "Artifact"
    else:
        return {"ok": False, "error": "Provide job_id or asset_path"}

    note = None
    if not asset_path:
        resolved = _resolve_absolute(path)
        if not resolved:
            # Path missing on disk (moved/cleaned) — post anyway; the card's
            # SafeMedia shows a broken-media state instead of silently
            # dropping the agent's result.
            note = f"Warning: file not found on disk: {path}"

    conn = connect()
    try:
        if job_id is not None:
            # Dedupe: the frontend auto-materializer ALSO creates an artifact
            # node on job.completed. Without this, a canvas ends up with two
            # identical cards for one job (observed live on canvas/65).
            existing = conn.execute(
                """
                SELECT id FROM canvas_node
                WHERE canvas_id = ? AND job_id = ? AND deleted = 0
                  AND type IN ('image', 'video')
                LIMIT 1
                """,
                (canvas_id, job_id),
            ).fetchone()
            if existing:
                return {
                    "ok": True,
                    "deduplicated": True,
                    "node_id": int(existing["id"]),
                    "canvas_node_id": int(existing["id"]),
                    "note": (
                        "This job's output is already on the canvas — no new card "
                        "created."
                    ),
                }
        x, y = next_artifact_slot(conn, canvas_id)
        cur = conn.execute(
            """
            INSERT INTO canvas_node
                (canvas_id, type, title, x, y, artifact_path, job_id, status,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'done', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (canvas_id, ntype, title, x, y, path, job_id),
        )
        node_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    await event_bus.publish(
        "canvas.updated", {"canvas_id": canvas_id, "reason": "node_created"}
    )
    return {
        "ok": True,
        "node_id": node_id,
        "canvas_node_id": node_id,
        "type": ntype,
        "title": title,
        "artifact_path": path,
        "position": {"x": x, "y": y},
        **({"warning": note} if note else {}),
        "note": "Posted to the canvas — it now shows as a card in the artifact column.",
    }


def review_node(
    project_id: int | None, session_id: int | None, args: dict[str, Any]
) -> dict[str, Any]:
    """Show the node's prompt and references. Does not queue a job."""
    node_id = as_int(args.get("node_id"))
    if node_id is None:
        return {"ok": False, "error": "node_id is required"}
    canvas = canvas_for(project_id, session_id)
    conn = connect()
    try:
        row = conn.execute(
            """
            SELECT id, title, workflow_id, input_values_json, artifact_path
            FROM canvas_node
            WHERE id = ? AND canvas_id = ? AND deleted = 0
            """,
            (node_id, canvas["id"]),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"ok": False, "error": f"Node {node_id} not found on this canvas"}
    try:
        values = json.loads(row["input_values_json"] or "{}")
    except json.JSONDecodeError:
        values = {}
    if not isinstance(values, dict):
        values = {}
    prompt = ""
    for value in values.values():
        if isinstance(value, str) and len(value.strip()) > len(prompt):
            prompt = value.strip()
    if not prompt:
        prompt = (row["title"] or "").strip()
    media = _media(values)
    if row["artifact_path"]:
        media["images"].append(row["artifact_path"])
    return {
        "ok": True,
        "awaiting_review": True,
        "scope": "render",
        "question": "Review the prompt and the references, then generate.",
        "options": ["Generate", "Revise"],
        "review": {
            "kind": "canvas",
            "items": [
                {
                    "id": int(row["id"]),
                    "entity": "canvas_node",
                    "canvas_id": int(canvas["id"]),
                    "name": row["title"] or "Node",
                    "prompt": prompt[:8000],
                    **media,
                }
            ],
        },
    }
