"""Render plugin: ComfyUI health, asset/video job enqueueing, job waiting."""
from __future__ import annotations

import asyncio
import json
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
            name="enqueue_asset_jobs",
            description=(
                "Queue character/location reference image generation from the "
                "saved image prompts. Call comfy_server_info first. "
                "missing_only=true (default) skips entities that already have "
                "images. Returns job ids — wait_for_jobs after."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "character_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Real character ids from get_workspace",
                    },
                    "location_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Real location ids from get_workspace",
                    },
                    "missing_only": {"type": "boolean"},
                    "workflow_id": {
                        "type": "integer",
                        "description": "Specific image workflow (from list_workflows)",
                    },
                    "asset_target": {
                        "type": "string",
                        "enum": ["sheet", "portrait"],
                        "description": "Character asset type to generate (default sheet)",
                    },
                },
            },
            executor=t_enqueue_asset_jobs,
            category="assets",
            requires_approval=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="enqueue_video_jobs",
            description=(
                "Queue video clip generation for scenes (ComfyUI video jobs with "
                "reference images wired automatically). Reference-based "
                "workflows need character sheets / location images to EXIST "
                "first — check get_workspace (✓img marks) and "
                "enqueue_asset_jobs + wait_for_jobs before this. Omit scene_ids "
                "to render all scenes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "scene_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Real scene ids from list_scenes; omit for all",
                    },
                    "workflow_id": {
                        "type": "integer",
                        "description": "Specific video workflow (from list_workflows)",
                    },
                },
            },
            executor=t_enqueue_video_jobs,
            category="video",
            requires_approval=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="run_workflow",
            description=(
                "Queue a Calliope ComfyUI workflow. Works in sandbox AND linked "
                "sessions. Sandbox jobs go to the hidden Playground scratch — "
                "do NOT create_project just to generate. Use this when the user "
                "message has a [Calliope context] appendix with workflow_id= "
                "(from an @workflow mention). Do NOT call list_workflows to "
                "guess the id. Call comfy_server_info first. prompt lands on "
                "(Input:prompt). "
                "width/height fill (Input:width)/(Input:height) by role. "
                "attachments are ordered local paths for (Input:image) / "
                "(Input:character) slots. Aspect ratio in the user's prose "
                "(e.g. 16:9) → 1920×1080 (1080p) or 1280×720 (720p); "
                "9:16 swaps those; 1:1 → 1080×1080. Returns job ids — "
                "wait_for_jobs after."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "workflow_id": {
                        "type": "integer",
                        "description": "Calliope workflow id from [Calliope context]",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Text for the (Input:prompt) node",
                    },
                    "width": {
                        "type": "integer",
                        "description": "Optional width for (Input:width) by role",
                    },
                    "height": {
                        "type": "integer",
                        "description": "Optional height for (Input:height) by role",
                    },
                    "input_values": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": "Optional extra nodeId → value overrides",
                    },
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ordered local asset paths for image/character slots",
                    },
                },
                "required": ["workflow_id"],
            },
            executor=t_run_workflow,
            category="assets",
            requires_project=False,
            requires_approval=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="list_jobs",
            description="List render jobs for the linked project (optionally by status).",
            parameters={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending", "running", "done", "failed"],
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
            },
            executor=t_list_jobs,
            category="video",
        )
    )
    registry.register(
        ToolDefinition(
            name="get_job_status",
            description="Get the current status of one render job.",
            parameters={
                "type": "object",
                "properties": {"job_id": {"type": "integer"}},
                "required": ["job_id"],
            },
            executor=t_get_job_status,
            category="video",
            requires_project=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="wait_for_jobs",
            description=(
                "Wait for render jobs to finish (polls until done/failed or "
                "timeout). ALWAYS call this after run_workflow / "
                "enqueue_asset_jobs / enqueue_video_jobs and report outcomes — "
                "failures include their error text. Pass job_ids from the "
                "enqueue result (required in sandbox). Before enqueue_video_jobs, "
                "make sure asset jobs are done (images exist)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "job_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Job ids from an enqueue result; omit for all active jobs",
                    },
                    "timeout_sec": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 86400,
                        "description": (
                            "Give up waiting after this many seconds. 0 = wait "
                            "indefinitely. Default is Settings → Queue poll "
                            "timeout (queue_poll_timeout_sec, 1800s / 30 min)."
                        ),
                    },
                },
            },
            executor=t_wait_for_jobs,
            category="video",
            requires_project=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="attach_asset",
            description=(
                "Add a generated image (from a job_id or a local path) to a "
                "project as a character sheet, environment, or misc. item. "
                "Works in sandbox — do not create_project just to file an "
                "image. Call list_projects first if the user named a film. "
                "Pass an existing character_id / location_id / item_id, or "
                "a name (matches an existing entity case-insensitively, "
                "otherwise creates one). job_id should be the "
                "run_workflow / Playground job that produced the file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "integer",
                        "description": "Completed generate job whose first output to attach",
                    },
                    "path": {
                        "type": "string",
                        "description": "Explicit asset path under assets_dir (optional if job_id)",
                    },
                    "project_id": {
                        "type": "integer",
                        "description": "Target film id (required in sandbox; defaults to linked project)",
                    },
                    "target": {
                        "type": "string",
                        "enum": ["character_sheet", "location", "item"],
                        "description": "Where to file the image",
                    },
                    "character_id": {"type": "integer"},
                    "location_id": {"type": "integer"},
                    "item_id": {"type": "integer"},
                    "name": {
                        "type": "string",
                        "description": "Entity name — updates a match, or creates if none",
                    },
                },
                "required": ["target"],
            },
            executor=t_attach_asset,
            requires_project=False,
            category="assets",
        )
    )
    registry.register(
        ToolDefinition(
            name="list_workflows",
            description=(
                "List enabled ComfyUI workflows (id, name, kind image/video, "
                "description)."
            ),
            parameters={"type": "object", "properties": {}},
            executor=t_list_workflows,
            requires_project=False,
            category="system",
        )
    )
    registry.register(
        ToolDefinition(
            name="comfy_server_info",
            description=(
                "Check the ComfyUI server connection (health check). REQUIRED "
                "before enqueue_asset_jobs / enqueue_video_jobs / run_workflow "
                "— if unreachable, tell the user and do not queue renders."
            ),
            parameters={"type": "object", "properties": {}},
            executor=t_comfy_server_info,
            requires_project=False,
            category="system",
        )
    )


# ── executors ───────────────────────────────────────────────────


async def t_enqueue_asset_jobs(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from calliope.agent.asset_agent import enqueue_asset_jobs as _enqueue

    jobs = await _enqueue(
        ctx.project_id,
        character_ids=args.get("character_ids"),
        asset_target=args.get("asset_target", "sheet"),
        location_ids=args.get("location_ids"),
        missing_only=bool(args.get("missing_only", True)),
        workflow_id=args.get("workflow_id"),
    )
    return {"jobs": jobs, "count": len(jobs)}


async def t_enqueue_video_jobs(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from calliope.agent.video_agent import enqueue_video_jobs as _enqueue

    try:
        jobs = await _enqueue(
            ctx.project_id,
            scene_ids=args.get("scene_ids"),
            workflow_id=args.get("workflow_id"),
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"jobs": jobs, "count": len(jobs)}


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _sandbox_attachment_paths(paths: list[str]) -> tuple[list[str], str | None]:
    """Resolve attachment paths under assets_dir. Returns (ok_paths, error)."""
    from pathlib import Path

    from calliope.config import settings

    assets_root = settings.assets_dir.resolve()
    out: list[str] = []
    for raw in paths:
        try:
            target = Path(raw).resolve()
            target.relative_to(assets_root)
        except (ValueError, OSError):
            return [], f"Attachment path outside assets directory: {raw}"
        if not target.is_file():
            return [], f"Attachment file not found: {raw}"
        out.append(str(target))
    return out, None


def _queue_project_id(ctx: ToolContext) -> int:
    """Linked project, or the hidden Playground scratch for sandbox generates."""
    if ctx.project_id is not None:
        return int(ctx.project_id)
    from calliope.routers.playground import ensure_playground_project

    conn = _db()
    try:
        return ensure_playground_project(conn)
    finally:
        conn.close()


async def t_run_workflow(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Enqueue one Calliope workflow (linked project, or Playground scratch)."""
    from calliope.comfyui.parser import parse_dynamic_inputs
    from calliope.comfyui.roles import input_has_role
    from calliope.comfyui.smart_fill import smart_fill_inputs
    from calliope.events.bus import event_bus
    from calliope.queue.manager import queue_manager

    queue_pid = _queue_project_id(ctx)
    sandbox = ctx.project_id is None
    try:
        workflow_id = int(args["workflow_id"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "workflow_id is required"}

    conn = _db()
    try:
        row = conn.execute(
            "SELECT id, name, kind, is_enabled, workflow_json FROM workflows WHERE id = ?",
            (workflow_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "Workflow not found"}
        if not row["is_enabled"]:
            return {"ok": False, "error": "Workflow is disabled"}
        kind = row["kind"]
        wf_name = row["name"]
        try:
            wf_json = json.loads(row["workflow_json"]) if row["workflow_json"] else {}
        except (json.JSONDecodeError, TypeError):
            return {"ok": False, "error": "Workflow JSON is invalid"}
    finally:
        conn.close()

    raw_paths = [str(p) for p in (args.get("attachments") or []) if p]
    safe_paths, path_err = _sandbox_attachment_paths(raw_paths)
    if path_err:
        return {"ok": False, "error": path_err}

    from pathlib import Path

    image_paths = [p for p in safe_paths if Path(p).suffix.lower() in _IMAGE_EXTS]
    extra: dict[str, Any] = {}
    iv = args.get("input_values")
    if isinstance(iv, dict):
        extra = {str(k): v for k, v in iv.items()}

    inputs = parse_dynamic_inputs(wf_json) if wf_json else []
    if args.get("width") is not None:
        for inp in inputs:
            if input_has_role(inp, "width"):
                extra[str(inp["nodeId"])] = int(args["width"])
                break
    if args.get("height") is not None:
        for inp in inputs:
            if input_has_role(inp, "height"):
                extra[str(inp["nodeId"])] = int(args["height"])
                break

    values = smart_fill_inputs(
        inputs,
        prompt=(str(args["prompt"]) if args.get("prompt") is not None else None),
        character_image=image_paths[0] if image_paths else None,
        location_image=image_paths[1] if len(image_paths) > 1 else None,
        ref_images=image_paths or None,
        extra=extra or None,
    )
    job = queue_manager.enqueue(
        project_id=queue_pid,
        kind=kind,
        workflow_id=workflow_id,
        scene_id=None,
        payload={"input_values": values, "source": "agent", "prompt": args.get("prompt")},
    )
    await event_bus.publish(
        "job.created",
        {
            "job_id": job["id"],
            "kind": kind,
            "source": "agent",
            "project_id": queue_pid,
        },
    )
    return {
        "jobs": [_sc_job_dict_lite(job)],
        "count": 1,
        "workflow_id": workflow_id,
        "workflow_name": wf_name,
        "sandbox": sandbox,
    }


def _first_job_output(job_id: int) -> tuple[str | None, str | None]:
    from calliope.queue.manager import queue_manager

    job = queue_manager.get_job(int(job_id))
    if not job:
        return None, f"Job {job_id} not found"
    raw = job.get("output_paths")
    if raw is None and job.get("output_paths_json"):
        try:
            raw = json.loads(job["output_paths_json"])
        except (json.JSONDecodeError, TypeError):
            raw = []
    paths = [p for p in (raw or []) if isinstance(p, str) and p.strip()]
    if not paths:
        return None, f"Job {job_id} has no output files yet (status={job.get('status')})"
    return paths[0], None


def _find_named(conn, table: str, project_id: int, name: str):
    return conn.execute(
        f"SELECT * FROM {table} WHERE project_id = ? AND LOWER(name) = LOWER(?)",
        (project_id, name),
    ).fetchone()


async def t_attach_asset(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """File a generated image onto a character / location / item of a real project."""
    from calliope.routers.playground import PLAYGROUND_STATUS

    target = str(args.get("target") or "")
    if target not in ("character_sheet", "location", "item"):
        return {"ok": False, "error": "target must be character_sheet, location, or item"}

    project_id = args.get("project_id")
    if project_id is None:
        project_id = ctx.project_id
    try:
        project_id = int(project_id)
    except (TypeError, ValueError):
        return {
            "ok": False,
            "error": "project_id is required in sandbox — list_projects then pass the film id.",
        }

    path = str(args.get("path") or "").strip()
    if not path:
        job_id = args.get("job_id")
        if job_id is None:
            return {"ok": False, "error": "Pass job_id or path"}
        path, err = _first_job_output(int(job_id))
        if err:
            return {"ok": False, "error": err}

    safe, path_err = _sandbox_attachment_paths([path])
    if path_err:
        return {"ok": False, "error": path_err}
    path = safe[0]

    conn = _db()
    try:
        project = conn.execute(
            "SELECT id, title, status FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if not project:
            return {"ok": False, "error": "Project not found"}
        if project["status"] == PLAYGROUND_STATUS or project["status"] == "system":
            return {"ok": False, "error": "Cannot attach to Playground scratch — pick a real film."}

        created = False
        entity: dict[str, Any] | None = None
        name = str(args.get("name") or "").strip()
        if target == "character_sheet":
            cid = args.get("character_id")
            row = None
            if cid is not None:
                row = conn.execute(
                    "SELECT * FROM characters WHERE id = ? AND project_id = ?",
                    (int(cid), project_id),
                ).fetchone()
                if not row:
                    return {"ok": False, "error": f"Character {cid} not found in this project"}
            elif name:
                row = _find_named(conn, "characters", project_id, name)
            else:
                return {"ok": False, "error": "character_id or name is required"}
            if row:
                conn.execute(
                    "UPDATE characters SET sheet_path = ? WHERE id = ?",
                    (path, int(row["id"])),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO characters (project_id, name, sheet_path) VALUES (?, ?, ?)",
                    (project_id, name, path),
                )
                created = True
                row = conn.execute("SELECT * FROM characters WHERE id = ?", (cur.lastrowid,)).fetchone()
            entity = row_to_dict(row)
            entity["sheet_path"] = path
        elif target == "location":
            lid = args.get("location_id")
            row = None
            if lid is not None:
                row = conn.execute(
                    "SELECT * FROM locations WHERE id = ? AND project_id = ?",
                    (int(lid), project_id),
                ).fetchone()
                if not row:
                    return {"ok": False, "error": f"Location {lid} not found in this project"}
            elif name:
                row = _find_named(conn, "locations", project_id, name)
            else:
                return {"ok": False, "error": "location_id or name is required"}
            if row:
                conn.execute(
                    "UPDATE locations SET reference_image_path = ? WHERE id = ?",
                    (path, int(row["id"])),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO locations (project_id, name, reference_image_path) "
                    "VALUES (?, ?, ?)",
                    (project_id, name, path),
                )
                created = True
                row = conn.execute("SELECT * FROM locations WHERE id = ?", (cur.lastrowid,)).fetchone()
            entity = row_to_dict(row)
            entity["reference_image_path"] = path
        else:
            iid = args.get("item_id")
            row = None
            if iid is not None:
                row = conn.execute(
                    "SELECT * FROM items WHERE id = ? AND project_id = ?",
                    (int(iid), project_id),
                ).fetchone()
                if not row:
                    return {"ok": False, "error": f"Item {iid} not found in this project"}
            elif name:
                row = _find_named(conn, "items", project_id, name)
            else:
                return {"ok": False, "error": "item_id or name is required"}
            if row:
                conn.execute(
                    "UPDATE items SET reference_image_path = ? WHERE id = ?",
                    (path, int(row["id"])),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO items (project_id, name, reference_image_path) VALUES (?, ?, ?)",
                    (project_id, name, path),
                )
                created = True
                row = conn.execute("SELECT * FROM items WHERE id = ?", (cur.lastrowid,)).fetchone()
            entity = row_to_dict(row)
            entity["reference_image_path"] = path

        conn.execute(
            "UPDATE projects SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (project_id,),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "project_id": project_id,
        "project_title": project["title"],
        "target": target,
        "path": path,
        "created": created,
        "entity": entity,
    }


async def t_list_jobs(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from calliope.queue.manager import queue_manager

    jobs = queue_manager.list_jobs(
        project_id=ctx.project_id,
        status=args.get("status"),
        limit=min(max(int(args.get("limit", 50)), 1), 200),
    )
    return {"jobs": [_sc_job_dict_lite(j) for j in jobs]}


def _sc_job_dict_lite(job: dict[str, Any]) -> dict[str, Any]:
    paths = job.get("output_paths")
    if paths is None and job.get("output_paths_json"):
        try:
            paths = json.loads(job["output_paths_json"])
        except (json.JSONDecodeError, TypeError):
            paths = []
    return {
        "id": job["id"],
        "kind": job["kind"],
        "status": job["status"],
        "scene_id": job.get("scene_id"),
        "error": job.get("error"),
        "output_paths": paths or [],
    }


async def t_get_job_status(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from calliope.queue.manager import queue_manager

    job = queue_manager.get_job(int(args["job_id"]))
    if not job or job.get("project_id") != _queue_project_id(ctx):
        return {"ok": False, "error": "Job not found in this project"}
    return _sc_job_dict_from_manager(job)


def _sc_job_dict_from_manager(job: dict[str, Any]) -> dict[str, Any]:
    for k in ("payload", "output_paths"):
        if job.get(f"{k}_json"):
            try:
                job[k] = json.loads(job[f"{k}_json"])
            except json.JSONDecodeError:
                pass
    return job


_WAIT_TIMEOUT_MAX = 86400.0  # matches SettingsUpdate.queue_poll_timeout_sec


def _resolve_wait_timeout(args: dict[str, Any]) -> float:
    """Seconds to wait. 0 = indefinitely.

    Default follows Settings → Queue poll timeout so the agent does not give
    up earlier than the worker polling ComfyUI. Explicit timeout_sec from the
    LLM is clamped (registry schema is not enforced at execute time).
    """
    from calliope.config import settings

    raw = args.get("timeout_sec")
    if raw is None:
        t = float(settings.queue_poll_timeout_sec)
        return 0.0 if t <= 0 else t
    t = float(raw)
    if t <= 0:
        return 0.0
    return min(max(t, 5.0), _WAIT_TIMEOUT_MAX)


async def t_wait_for_jobs(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from calliope.queue.manager import queue_manager

    job_ids = [int(j) for j in (args.get("job_ids") or [])]
    timeout = _resolve_wait_timeout(args)
    deadline = None if timeout <= 0 else asyncio.get_event_loop().time() + timeout
    queue_pid = _queue_project_id(ctx)
    while True:
        jobs: list[dict[str, Any]] = []
        invalid: list[int] = []
        if job_ids:
            for jid in job_ids:
                job = queue_manager.get_job(jid)
                if job and job.get("project_id") == queue_pid:
                    jobs.append(job)
                else:
                    invalid.append(jid)
        else:
            jobs = [
                j
                for j in queue_manager.list_jobs(project_id=queue_pid)
                if j["status"] in ("pending", "running")
            ]
            if not jobs:
                recent = queue_manager.list_jobs(project_id=queue_pid, limit=10)
                return {"jobs": [_sc_job_dict_lite(j) for j in recent], "waited": False}
        active = [j for j in jobs if j["status"] in ("pending", "running")]
        if not active:
            # Report invalid ids instead of silently dropping them: an
            # all-invalid request must not look like "everything finished".
            out = {"jobs": [_sc_job_dict_lite(j) for j in jobs], "waited": True}
            if invalid:
                out["not_found"] = invalid
            return out
        if deadline is not None and asyncio.get_event_loop().time() > deadline:
            return {
                "ok": False,
                "error": f"Timed out after {timeout}s waiting for {len(active)} job(s)",
                "jobs": [_sc_job_dict_lite(j) for j in active],
            }
        await asyncio.sleep(3)


async def t_list_workflows(ctx: ToolContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    conn = _db()
    try:
        rows = conn.execute(
            """
            SELECT id, name, kind, description, prompt_profile, is_enabled
            FROM workflows WHERE is_enabled = 1 ORDER BY kind, id
            """
        ).fetchall()
        return [row_to_dict(r) for r in rows]
    finally:
        conn.close()


async def t_comfy_server_info(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from calliope.comfyui.client import ComfyUIClient

    client = ComfyUIClient()
    try:
        info = await client.health()
        return {"ok": True, "comfyui": info}
    finally:
        await client.close()
