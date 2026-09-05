"""Canvas plugin: the agent reads the preview board and posts artifacts to it.

The canvas (project story-bible or sandbox playground) is the merged surface
for both chat modes. Generation happens in chat; these tools let the agent
inspect the board, drop job outputs onto it as cards, and manage cards.
"""
from __future__ import annotations

import json
import re
from typing import Any

from calliope.agent.harness.registry import (
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    _db,
)
from calliope.db import row_to_dict


def _canvas_for_session(ctx: ToolContext, *, create: bool = True) -> tuple[int | None, str | None]:
    """Resolve the session's canvas: project canvas for linked sessions,
    sandbox canvas for blind ones. Returns (canvas_id, error).

    Sandbox canvases are created on demand (get-or-create): free-flow
    generation is the whole point of a sandbox, so posting an artifact must
    never dead-end on "no canvas yet" and push the model toward attaching
    to a real film.
    """
    conn = _db()
    try:
        if ctx.project_id is not None:
            row = conn.execute(
                "SELECT id FROM canvas WHERE project_id = ? ORDER BY id DESC LIMIT 1",
                (int(ctx.project_id),),
            ).fetchone()
            if not row:
                return (
                    None,
                    "This project has no canvas yet — open the AI Canvas page to create it.",
                )
            return int(row["id"]), None
        row = conn.execute(
            """
            SELECT id FROM canvas WHERE agent_session_id = ? AND project_id IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            (int(ctx.session_id),),
        ).fetchone()
        if row:
            return int(row["id"]), None
        if not create:
            return None, "No sandbox canvas for this session yet."
        from calliope.routers.canvas import CanvasCreate, _derive_canvas_title

        payload = CanvasCreate(agent_session_id=int(ctx.session_id))
        cur = conn.execute(
            """
            INSERT INTO canvas (agent_session_id, title, created_at, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (int(ctx.session_id), _derive_canvas_title(conn, payload)),
        )
        canvas_id = int(cur.lastrowid)
        conn.commit()
        return canvas_id, None
    finally:
        conn.close()


def _node_summary(node: dict[str, Any]) -> dict[str, Any]:
    out = {
        "canvas_node_id": node["id"],
        "type": node["type"],
        "title": node["title"],
    }
    if node.get("entity_type"):
        out["entity"] = f"{node['entity_type']}#{node['entity_id']}"
    if node.get("workflow_id"):
        out["workflow_id"] = node["workflow_id"]
    if node.get("artifact_path"):
        out["artifact_path"] = node["artifact_path"]
    if node.get("job_id"):
        out["job_id"] = node["job_id"]
    if node.get("status") and node["status"] != "idle":
        out["status"] = node["status"]
    return out


async def t_summarize_canvas(ctx: ToolContext, _args: dict[str, Any]) -> dict[str, Any]:
    """Nodes + both edge kinds so the model can reason about the graph."""
    canvas_id, err = _canvas_for_session(ctx)
    if err:
        return {"ok": False, "error": err}
    conn = _db()
    try:
        nodes = [
            row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM canvas_node WHERE canvas_id = ? AND deleted = 0 ORDER BY id",
                (canvas_id,),
            ).fetchall()
        ]
        node_ids = {n["id"] for n in nodes}
        edges = [
            row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM canvas_edge WHERE canvas_id = ? ORDER BY id",
                (canvas_id,),
            ).fetchall()
        ]
        # Resolve display titles in one pass.
        titles = {n["id"]: n["title"] for n in nodes}

        def _name(nid: int) -> str:
            return titles.get(nid) or f"node#{nid}"

        data_edges = []
        link_edges = []
        for e in edges:
            if e["src_node_id"] not in node_ids or e["dst_node_id"] not in node_ids:
                continue
            entry = {
                "from": _name(e["src_node_id"]),
                "to": _name(e["dst_node_id"]),
            }
            if e["kind"] == "data":
                entry["dst_role"] = e["dst_role"]
                entry["dst_comfy_node_id"] = e["dst_comfy_node_id"]
                data_edges.append(entry)
            else:
                entry["label"] = e["label"]
                link_edges.append(entry)
    finally:
        conn.close()
    return {
        "ok": True,
        "canvas_id": canvas_id,
        "nodes": [_node_summary(n) for n in nodes],
        "data_edges": data_edges,
        "link_edges": link_edges,
        "counts": {"nodes": len(nodes), "data": len(data_edges), "links": len(link_edges)},
    }


async def t_create_node(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Add a workflow or artifact node to the canvas. Entity nodes auto-seed."""
    canvas_id, err = _canvas_for_session(ctx)
    if err:
        return {"ok": False, "error": err}
    ntype = str(args.get("type") or "").strip()
    if ntype not in ("workflow", "image", "video"):
        return {"ok": False, "error": "type must be 'workflow', 'image', or 'video'"}
    workflow_id = args.get("workflow_id")
    artifact_path = args.get("artifact_path")
    if ntype == "workflow":
        if workflow_id is None:
            return {
                "ok": False,
                "error": "workflow nodes require workflow_id (from list_workflows)",
            }
        conn = _db()
        try:
            wf = conn.execute(
                "SELECT id, name FROM workflows WHERE id = ?", (int(workflow_id),)
            ).fetchone()
        finally:
            conn.close()
        if not wf:
            return {"ok": False, "error": f"Workflow {workflow_id} not found"}
        title = str(args.get("title") or wf["name"])
    else:
        if not artifact_path:
            return {"ok": False, "error": f"{ntype} nodes require artifact_path"}
        title = str(args.get("title") or "Artifact")

    conn = _db()
    try:
        cur = conn.execute(
            """
            INSERT INTO canvas_node
                (canvas_id, type, title, x, y, workflow_id, artifact_path,
                 status, input_values_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'idle', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                canvas_id,
                ntype,
                title,
                float(args.get("x") or 0),
                float(args.get("y") or 0),
                workflow_id if ntype == "workflow" else None,
                artifact_path if ntype in ("image", "video") else None,
            ),
        )
        node_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": True,
        "canvas_node_id": node_id,
        "type": ntype,
        "title": title,
        "note": "Node added. Connect it with canvas_connect (data) or canvas_link.",
    }


async def t_connect(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Data edge: source artifact/entity feeds a workflow input role."""
    canvas_id, err = _canvas_for_session(ctx)
    if err:
        return {"ok": False, "error": err}
    try:
        src = int(args["src_node_id"])
        dst = int(args["dst_node_id"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "src_node_id and dst_node_id are required"}
    dst_role = str(args.get("dst_role") or "").strip()
    if not dst_role:
        return {
            "ok": False,
            "error": "dst_role is required (the workflow input role, e.g. 'image')",
        }

    conn = _db()
    try:
        src_row = conn.execute(
            """
            SELECT type, artifact_path, entity_type FROM canvas_node
            WHERE id = ? AND canvas_id = ?
            """,
            (src, canvas_id),
        ).fetchone()
        dst_row = conn.execute(
            "SELECT type, workflow_id FROM canvas_node WHERE id = ? AND canvas_id = ?",
            (dst, canvas_id),
        ).fetchone()
        if not src_row or not dst_row:
            return {"ok": False, "error": "Node not found on this canvas"}
        if dst_row["type"] != "workflow" or not dst_row["workflow_id"]:
            return {"ok": False, "error": "Data edges must land on a workflow node"}

        from calliope.comfyui.parser import parse_dynamic_inputs

        wf_json_row = conn.execute(
            "SELECT workflow_json FROM workflows WHERE id = ?", (dst_row["workflow_id"],)
        ).fetchone()
        wf_json = (
            json.loads(wf_json_row["workflow_json"])
            if wf_json_row and wf_json_row["workflow_json"]
            else {}
        )
        inputs = parse_dynamic_inputs(wf_json)

        from calliope.comfyui.roles import input_has_role

        comfy_node_id: str | None = None
        target = args.get("dst_comfy_node_id")
        if target is not None:
            comfy_node_id = str(target)
        else:
            for inp in inputs:
                if input_has_role(inp, dst_role):
                    comfy_node_id = str(inp["nodeId"])
                    break
        if comfy_node_id is None:
            roles = sorted(
                {
                    str(inp["role"])
                    for inp in inputs
                    if inp.get("role")
                }
            )
            return {
                "ok": False,
                "error": f"Workflow #{dst_row['workflow_id']} has no input with role '{dst_role}'. "
                f"Available roles: {roles or 'none tagged'}.",
            }
        dup = conn.execute(
            """
            SELECT id FROM canvas_edge
            WHERE canvas_id = ? AND src_node_id = ? AND dst_node_id = ? AND kind = 'data'
            """,
            (canvas_id, src, dst),
        ).fetchone()
        if dup:
            return {"ok": False, "error": "A data edge between these nodes already exists"}
        cur = conn.execute(
            """
            INSERT INTO canvas_edge
                (canvas_id, src_node_id, dst_node_id, kind, dst_role,
                 dst_comfy_node_id, created_at)
            VALUES (?, ?, ?, 'data', ?, ?, CURRENT_TIMESTAMP)
            """,
            (canvas_id, src, dst, dst_role, comfy_node_id),
        )
        edge_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": True,
        "edge_id": edge_id,
        "src_node_id": src,
        "dst_node_id": dst,
        "dst_role": dst_role,
        "dst_comfy_node_id": comfy_node_id,
        "note": "Connected. run_canvas_node on the workflow node will use this input.",
    }


async def t_link_nodes(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Link edge: a labeled story-bible relationship between two nodes."""
    canvas_id, err = _canvas_for_session(ctx)
    if err:
        return {"ok": False, "error": err}
    try:
        src = int(args["src_node_id"])
        dst = int(args["dst_node_id"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "src_node_id and dst_node_id are required"}
    label = str(args.get("label") or "").strip()
    if not label:
        return {"ok": False, "error": "label is required (e.g. 'rival of')"}
    if len(label) > 80:
        return {"ok": False, "error": "label too long (max 80 characters)"}
    conn = _db()
    try:
        for nid in (src, dst):
            if not conn.execute(
                "SELECT id FROM canvas_node WHERE id = ? AND canvas_id = ?",
                (nid, canvas_id),
            ).fetchone():
                return {"ok": False, "error": f"Node {nid} not found on this canvas"}
        dup = conn.execute(
            """
            SELECT id FROM canvas_edge
            WHERE canvas_id = ? AND src_node_id = ? AND dst_node_id = ? AND kind = 'link'
            """,
            (canvas_id, src, dst),
        ).fetchone()
        if dup:
            return {"ok": False, "error": "A link edge between these nodes already exists"}
        cur = conn.execute(
            """
            INSERT INTO canvas_edge
                (canvas_id, src_node_id, dst_node_id, kind, label, created_at)
            VALUES (?, ?, ?, 'link', ?, CURRENT_TIMESTAMP)
            """,
            (canvas_id, src, dst, label),
        )
        edge_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "edge_id": edge_id, "label": label}


async def t_update_node(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Rename / reposition a canvas node, or set a workflow node's manual inputs."""
    canvas_id, err = _canvas_for_session(ctx)
    if err:
        return {"ok": False, "error": err}
    try:
        node_id = int(args["node_id"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "node_id is required"}
    sets: list[str] = []
    params: list[Any] = []
    title = args.get("title")
    if title is not None:
        sets.append("title = ?")
        params.append(str(title))
    x = args.get("x")
    if x is not None:
        sets.append("x = ?")
        params.append(float(x))
    y = args.get("y")
    if y is not None:
        sets.append("y = ?")
        params.append(float(y))
    input_values = args.get("input_values")
    if isinstance(input_values, dict):
        # input_values keys are workflow-internal Comfy node ids ("12").
        conn = _db()
        try:
            node = conn.execute(
                "SELECT input_values_json FROM canvas_node WHERE id = ? AND canvas_id = ?",
                (node_id, canvas_id),
            ).fetchone()
        finally:
            conn.close()
        if node is None:
            return {"ok": False, "error": "Node not found on this canvas"}
        try:
            current = json.loads(node["input_values_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            current = {}
        current.update({str(k): v for k, v in input_values.items()})
        sets.append("input_values_json = ?")
        params.append(json.dumps(current))
    if not sets:
        return {"ok": False, "error": "Nothing to update (title / x / y / input_values)"}
    sets.append("updated_at = CURRENT_TIMESTAMP")
    params.extend([node_id, canvas_id])
    conn = _db()
    try:
        cur = conn.execute(
            f"UPDATE canvas_node SET {', '.join(sets)} WHERE id = ? AND canvas_id = ?",
            params,
        )
        if cur.rowcount == 0:
            return {"ok": False, "error": "Node not found on this canvas"}
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "node_id": node_id}


async def t_run_canvas_node(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Run a workflow node: its data edges become input_values, then enqueue."""
    from calliope.agent.harness.plugins.render import _queue_project_id
    from calliope.comfyui.parser import parse_dynamic_inputs
    from calliope.events.bus import event_bus
    from calliope.queue.manager import queue_manager

    canvas_id, err = _canvas_for_session(ctx)
    if err:
        return {"ok": False, "error": err}
    try:
        node_id = int(args["node_id"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "node_id is required (a workflow node)"}

    conn = _db()
    try:
        node = conn.execute(
            "SELECT * FROM canvas_node WHERE id = ? AND canvas_id = ?",
            (node_id, canvas_id),
        ).fetchone()
        if not node:
            return {"ok": False, "error": "Node not found on this canvas"}
        if node["type"] != "workflow" or not node["workflow_id"]:
            return {"ok": False, "error": "run_canvas_node targets a workflow node"}
        wf = conn.execute(
            "SELECT id, name, kind, is_enabled, workflow_json FROM workflows WHERE id = ?",
            (node["workflow_id"],),
        ).fetchone()
        if not wf:
            return {"ok": False, "error": "Workflow not found"}
        if not wf["is_enabled"]:
            return {"ok": False, "error": "Workflow is disabled"}
        try:
            wf_json = json.loads(wf["workflow_json"]) if wf["workflow_json"] else {}
        except (json.JSONDecodeError, TypeError):
            return {"ok": False, "error": "Workflow JSON is invalid"}

        inputs = parse_dynamic_inputs(wf_json)
        from calliope.comfyui.roles import normalize_input_role

        # Start from persisted manual inputs (form values / agent-set values).
        try:
            values: dict[str, Any] = json.loads(node["input_values_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            values = {}

        # Data edges into this node inject media paths per resolved role.
        edges = conn.execute(
            "SELECT * FROM canvas_edge WHERE canvas_id = ? AND kind = 'data' AND dst_node_id = ?",
            (canvas_id, node_id),
        ).fetchall()
        injected: list[str] = []
        missing_roles: list[str] = []
        for e in edges:
            src = conn.execute(
                "SELECT artifact_path, entity_type FROM canvas_node WHERE id = ?",
                (e["src_node_id"],),
            ).fetchone()
            path = src["artifact_path"] if src else None
            if not path and src and src["entity_type"] in ("character", "location", "item"):
                path = _entity_image_path(conn, src["entity_type"], canvas_id)
            role = normalize_input_role(e["dst_role"])
            comfy_id = e["dst_comfy_node_id"]
            if path and comfy_id:
                values[str(comfy_id)] = path
                injected.append(f"{role or 'input'} <- {path}")
            elif role:
                missing_roles.append(role)

        # Untyped/slot fills: anything a required input still expects.
        required_missing = [
            str(inp["nodeId"])
            for inp in inputs
            if inp.get("required") and str(inp["nodeId"]) not in values
        ]
    finally:
        conn.close()

    if missing_roles and not injected:
        return {
            "ok": False,
            "error": f"No connected inputs could be resolved for roles {missing_roles}. "
            "Connect an image/video node with canvas_connect first.",
        }

    queue_pid = _queue_project_id(ctx)
    job = queue_manager.enqueue(
        project_id=queue_pid,
        kind=wf["kind"],
        workflow_id=wf["id"],
        scene_id=None,
        payload={
            "input_values": values,
            "source": "canvas",
            "canvas_id": canvas_id,
            "canvas_node_id": node_id,
        },
    )
    conn = _db()
    try:
        conn.execute(
            """
            UPDATE canvas_node SET status = 'running', job_id = ?,
                   updated_at = CURRENT_TIMESTAMP WHERE id = ?
            """,
            (job["id"], node_id),
        )
        conn.commit()
    finally:
        conn.close()
    await event_bus.publish(
        "job.created",
        {
            "job_id": job["id"],
            "kind": wf["kind"],
            "source": "canvas",
            "project_id": queue_pid,
        },
    )
    return {
        "ok": True,
        "job_id": job["id"],
        "workflow": wf["name"],
        "canvas_node_id": node_id,
        "injected_inputs": injected,
        "missing_required_inputs": required_missing,
        "note": "Queued. Use wait_for_jobs; the artifact lands as a canvas node when done.",
    }


def _entity_image_path(conn, entity_type: str, canvas_id: int) -> str | None:
    """Latest reference image for the entity behind ANY node on this canvas —
    entity nodes don't store artifact paths; their media resolves live."""
    canvas = conn.execute(
        "SELECT project_id FROM canvas WHERE id = ?", (canvas_id,)
    ).fetchone()
    pid = canvas["project_id"] if canvas else None
    if pid is None:
        return None
    if entity_type == "character":
        row = conn.execute(
            """
            SELECT sheet_path, portrait_path FROM characters
            WHERE project_id = ? AND (sheet_path IS NOT NULL OR portrait_path IS NOT NULL)
            ORDER BY id DESC LIMIT 1
            """,
            (pid,),
        ).fetchone()
        if row:
            return row["sheet_path"] or row["portrait_path"]
    elif entity_type == "location":
        row = conn.execute(
            """
            SELECT reference_image_path FROM locations
            WHERE project_id = ? AND reference_image_path IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
            (pid,),
        ).fetchone()
        if row:
            return row["reference_image_path"]
    elif entity_type == "item":
        row = conn.execute(
            """
            SELECT reference_image_path FROM items
            WHERE project_id = ? AND reference_image_path IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
            (pid,),
        ).fetchone()
        if row:
            return row["reference_image_path"]
    return None


async def t_post_artifact(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Drop a job output (or an existing asset path) onto the canvas as a card."""
    from calliope.routers.canvas import next_artifact_slot

    canvas_id, err = _canvas_for_session(ctx)
    if err:
        return {"ok": False, "error": err}

    job_id = args.get("job_id")
    asset_path = str(args.get("asset_path") or "").strip()
    title = str(args.get("title") or "").strip()

    if job_id is not None:
        conn = _db()
        try:
            job = conn.execute(
                "SELECT id, kind, status, output_paths_json FROM jobs WHERE id = ?",
                (int(job_id),),
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
        ntype = "video" if re.search(r"\.(mp4|webm|mov|avi)$", path, re.IGNORECASE) else "image"
        if not title:
            title = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or "Artifact"
    else:
        return {"ok": False, "error": "Provide job_id or asset_path"}

    if not asset_path:
        resolved = _resolve_absolute(path)
        if not resolved:
            # Path missing on disk (moved/cleaned) — post anyway; the card's
            # SafeMedia shows a broken-media state instead of silently
            # dropping the agent's result.
            title = (title or path.replace("\\", "/").rsplit("/", 1)[-1] or "Artifact")
            note = f"Warning: file not found on disk: {path}"
        else:
            note = None
    else:
        note = None

    conn = _db()
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
                (canvas_id, int(job_id)),
            ).fetchone()
            if existing:
                return {
                    "ok": True,
                    "deduplicated": True,
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
            (canvas_id, ntype, title, x, y, path, int(job_id) if job_id is not None else None),
        )
        node_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    from calliope.events.bus import event_bus

    await event_bus.publish("canvas.updated", {"canvas_id": canvas_id, "reason": "node_created"})
    return {
        "ok": True,
        "canvas_node_id": node_id,
        "type": ntype,
        "title": title,
        "artifact_path": path,
        "position": {"x": x, "y": y},
        **({"warning": note} if note else {}),
        "note": "Posted to the canvas — it now shows as a card in the artifact column.",
    }


def _resolve_absolute(path: str) -> str | None:
    """Paths in jobs.output_paths / asset columns are absolute (and rebased on
    folder moves). Accept the stored value or a basename under assets_dir."""
    from pathlib import Path

    from calliope.config import settings

    candidates = [Path(path), Path(settings.assets_dir) / path.replace("\\", "/").lstrip("/\\")]
    for c in candidates:
        try:
            if c.is_file():
                return str(c)
        except OSError:
            continue
    return None


async def t_delete_node(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Remove a node (entity nodes are tombstoned; the story bible is safe)."""
    canvas_id, err = _canvas_for_session(ctx)
    if err:
        return {"ok": False, "error": err}
    try:
        node_id = int(args["node_id"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "node_id is required"}
    conn = _db()
    try:
        node = conn.execute(
            "SELECT id, type FROM canvas_node WHERE id = ? AND canvas_id = ? AND deleted = 0",
            (node_id, canvas_id),
        ).fetchone()
        if not node:
            return {"ok": False, "error": "Node not found on this canvas"}
        conn.execute(
            "DELETE FROM canvas_edge WHERE src_node_id = ? OR dst_node_id = ?",
            (node_id, node_id),
        )
        if node["type"] == "entity":
            conn.execute(
                "UPDATE canvas_node SET deleted = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (node_id,),
            )
        else:
            conn.execute("DELETE FROM canvas_node WHERE id = ?", (node_id,))
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": True,
        "node_id": node_id,
        "note": "Removed from the canvas (edges attached to it were deleted too).",
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="summarize_canvas",
            description=(
                "See the AI Canvas for this session — project canvases AND "
                "sandbox canvases (every sandbox session gets its own board). "
                "Every node (entities, artifacts, workflows) plus data edges "
                "(workflow input wiring) and link edges (labeled "
                "relationships). Call before any canvas edit."
            ),
            parameters={"type": "object", "properties": {}},
            executor=t_summarize_canvas,
            category="canvas",
            requires_project=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="create_canvas_node",
            description=(
                "Add a node to the canvas. type='workflow' needs workflow_id from "
                "list_workflows; type='image'/'video' needs artifact_path. Entity "
                "cards (characters/locations/items/scenes) auto-seed and cannot be created."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["workflow", "image", "video"],
                    },
                    "workflow_id": {
                        "type": "integer",
                        "description": "Required for type='workflow' (from list_workflows)",
                    },
                    "artifact_path": {
                        "type": "string",
                        "description": "Required for image/video nodes",
                    },
                    "title": {"type": "string"},
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                },
                "required": ["type"],
            },
            executor=t_create_node,
            category="canvas",
            requires_project=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="canvas_connect",
            description=(
                "Wire a data edge: a source node's image/video feeds a workflow "
                "node's input role (e.g. dst_role='image' for an (Input:image) "
                "slot, 'video' for extend, 'prompt' for text). The Comfy node id "
                "is resolved from the workflow's role tags."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "src_node_id": {
                        "type": "integer",
                        "description": "canvas_node_id of the source (image/video/entity)",
                    },
                    "dst_node_id": {
                        "type": "integer",
                        "description": "canvas_node_id of the workflow node",
                    },
                    "dst_role": {
                        "type": "string",
                        "description": "Workflow input role: prompt/negative/image/video/audio/...",
                    },
                    "dst_comfy_node_id": {
                        "type": "string",
                        "description": "Optional explicit Comfy node id inside the workflow JSON",
                    },
                },
                "required": ["src_node_id", "dst_node_id", "dst_role"],
            },
            executor=t_connect,
            category="canvas",
            requires_project=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="canvas_link",
            description=(
                "Annotate a relationship between two nodes on the canvas — e.g. "
                "character rivals location, item belongs to character. Label is "
                "free text (max 80 chars), shown on the graph edge."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "src_node_id": {"type": "integer"},
                    "dst_node_id": {"type": "integer"},
                    "label": {"type": "string", "description": "e.g. 'rival of'"},
                },
                "required": ["src_node_id", "dst_node_id", "label"],
            },
            executor=t_link_nodes,
            category="canvas",
            requires_project=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="update_canvas_node",
            description=(
                "Rename or move a canvas node, or set manual input_values "
                "(keyed by workflow-internal Comfy node id) on a workflow node."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "node_id": {"type": "integer", "description": "canvas_node_id"},
                    "title": {"type": "string"},
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "input_values": {
                        "type": "object",
                        "description": "Merged into the node's persisted input_values",
                    },
                },
                "required": ["node_id"],
            },
            executor=t_update_node,
            category="canvas",
            requires_project=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="run_canvas_node",
            description=(
                "Run a workflow node on the canvas: its connected data edges "
                "become input_values (role-resolved media paths), then the job "
                "enqueues exactly like Playground. Expensive — ask first."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "node_id": {
                        "type": "integer",
                        "description": "canvas_node_id of the workflow node",
                    },
                },
                "required": ["node_id"],
            },
            executor=t_run_canvas_node,
            category="canvas",
            requires_project=False,
            requires_approval=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="post_artifact_to_canvas",
            description=(
                "Drop a finished generation onto the AI Canvas as a card — "
                "works in BOTH modes: the session's sandbox board (free-flow "
                "generation) and the project board. Give job_id (from "
                "enqueue/wait_for_jobs) or an existing asset_path. The card "
                "stacks in the artifact column; the user sees it render "
                "immediately. ALWAYS call this after any image/video the user "
                "asked for — never tell a sandbox user the canvas is "
                "project-only, and never attach to a project unless they asked."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "integer",
                        "description": "A completed job id — its first output is posted",
                    },
                    "asset_path": {
                        "type": "string",
                        "description": "Path to an existing asset file (image or video)",
                    },
                    "title": {"type": "string", "description": "Card title (optional)"},
                },
            },
            executor=t_post_artifact,
            category="canvas",
            requires_project=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="delete_canvas_node",
            description=(
                "Remove a node from the canvas and its edges. Entity cards are "
                "only hidden (the story bible is untouched); workflow/artifact "
                "nodes are deleted. Confirm with the user first."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "node_id": {"type": "integer", "description": "canvas_node_id"},
                },
                "required": ["node_id"],
            },
            executor=t_delete_node,
            category="canvas",
            requires_project=False,
        )
    )
