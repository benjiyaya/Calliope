"""Background queue worker loop."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pathlib import Path as _fs_path

from calliope import config
from calliope.comfyui.client import ComfyUIClient, select_output_files
from calliope.comfyui.dry_run import write_placeholder_mp4, write_placeholder_png
from calliope.comfyui.parser import parse_dynamic_inputs
from calliope.comfyui.patcher import node_widget_field, patch_workflow
from calliope.comfyui.roles import normalize_input_role
from calliope.db import get_db
from calliope.events.bus import event_bus
from calliope.export.runner import run_export
from calliope.queue.manager import queue_manager

logger = logging.getLogger("calliope.worker")


class _CancelledByUser(RuntimeError):
    """The job row was flipped to 'cancelled' out-of-band (Stop button).
    The row already carries the terminal state; the loop must not overwrite
    it with mark_failed (which would bump retry_count and emit job.failed)."""


# Exposed (Input:*) slots carrying prompt text or a reference. Strict mode drops
# the ones the workflow file itself supplies, so only the LLM prompt and
# user-provided references survive.
_LEAKY_ROLES = frozenset({"prompt", "negative", "image", "audio", "video", "character", "location"})
_LEAKY_KINDS = frozenset({"image", "image_url", "audio", "video"})
_MEDIA_ROLES = frozenset({"image", "audio", "video", "character", "location"})
_MEDIA_KINDS = frozenset({"image", "image_url", "audio", "video"})


def _is_leaky_input(inp: dict[str, Any]) -> bool:
    return (
        normalize_input_role(inp.get("role")) in _LEAKY_ROLES
        or inp.get("kind") in _LEAKY_KINDS
    )


def _is_media_input(inp: dict[str, Any]) -> bool:
    return (
        normalize_input_role(inp.get("role")) in _MEDIA_ROLES
        or inp.get("kind") in _MEDIA_KINDS
    )


def _links_into(value: Any, removed: set[str]) -> bool:
    """True when a ComfyUI link ``[node_id, output]`` targets a removed node."""
    if isinstance(value, list):
        if (
            len(value) >= 2
            and isinstance(value[0], str)
            and value[0] in removed
            and isinstance(value[1], int)
        ):
            return True
        return any(_links_into(item, removed) for item in value)
    if isinstance(value, dict):
        return any(_links_into(item, removed) for item in value.values())
    return False


def _prune_nodes(nodes: dict[str, Any], root_ids: list[str]) -> None:
    """Remove nodes and their wiring, cascading to nodes left with no inputs.

    A removed media loader takes its links with it; a pass-through such as
    GetVideoComponents, left with nothing to consume, is removed too so ComfyUI
    never sees a dangling reference.
    """
    removed = {str(r) for r in root_ids}
    changed = True
    while changed:
        changed = False
        for nid, node in list(nodes.items()):
            if str(nid) in removed or not isinstance(node, dict):
                continue
            inputs = node.get("inputs") or {}
            if not inputs:
                continue
            kept = {k: v for k, v in inputs.items() if not _links_into(v, removed)}
            if len(kept) == len(inputs):
                continue
            node["inputs"] = kept
            changed = True
            if not kept:
                removed.add(str(nid))
    for nid in removed:
        nodes.pop(nid, None)


def _apply_strict_mode(
    patched: dict[str, Any],
    template: dict[str, Any],
    schema: list[dict[str, Any]],
    provided: dict[str, Any],
) -> dict[str, Any]:
    """Strict mode: drop the workflow file's own prompt/reference inputs.

    Only the LLM prompt and user-provided references are kept — their value
    differs from the template's. A leaky slot whose value is the file's own
    baked default (smart-fill re-injects it) or is absent is dropped: a media
    reference has its node and wiring pruned so the graph runs without it, while
    a prompt/negative text slot is blanked (pruning it would break its encoder).
    """
    drop: list[str] = []
    for inp in schema:
        if not _is_leaky_input(inp):
            continue
        nid = str(inp.get("nodeId"))
        node = patched.get(nid)
        if not isinstance(node, dict):
            continue
        field = node_widget_field(node)
        tpl = template.get(nid)
        template_value = (tpl.get("inputs") or {}).get(field) if isinstance(tpl, dict) else None
        value = provided.get(nid)
        blank = value is None or (isinstance(value, str) and not value.strip())
        if not blank and str(value) != str(template_value):
            continue  # a genuine user / LLM / context value
        if _is_media_input(inp):
            drop.append(nid)
        else:
            inputs = dict(node.get("inputs") or {})
            inputs[field] = ""
            node["inputs"] = inputs
    if drop:
        _prune_nodes(patched, drop)
    return patched


class QueueWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        reset = queue_manager.reset_stale_jobs()
        if reset:
            logger.info("Reset %s stale running jobs to pending", reset)
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="calliope-queue-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.wait([self._task], timeout=5)

    async def _loop(self) -> None:
        logger.info("Queue worker started")
        while not self._stop.is_set():
            try:
                if queue_manager.paused:
                    await asyncio.sleep(config.settings.queue_poll_interval_sec)
                    continue
                job = queue_manager.claim_next()
                if not job:
                    await asyncio.sleep(config.settings.queue_poll_interval_sec)
                    continue
                await event_bus.publish(
                    "job.started",
                    {
                        "job_id": job["id"],
                        "kind": job["kind"],
                        "project_id": job.get("project_id"),
                        "message": self._job_label(job),
                    },
                )
                try:
                    outputs = await self._run_job(job)
                    queue_manager.mark_done(job["id"], outputs)
                    if job["kind"] == "export":
                        self._mark_project_completed(job["project_id"])
                    # Prompt snippet for canvas artifact labels: users
                    # compare multiple generations of the same scene
                    # side by side and pick one to re-apply. Parse the
                    # stored payload here — _run_job's parsed copy is not
                    # in this scope.
                    try:
                        job_payload = json.loads(job["payload_json"] or "{}")
                    except (json.JSONDecodeError, TypeError):
                        job_payload = {}
                    await event_bus.publish(
                        "job.completed",
                        {
                            "job_id": job["id"],
                            "kind": job["kind"],
                            "outputs": outputs,
                            "project_id": job["project_id"],
                            "prompt": str(job_payload.get("prompt") or "")[:120],
                            # Source stamp lets the UI attribute sandbox jobs
                            # to their enqueuer without racing the jobs poll.
                            "source": job_payload.get("source"),
                            "message": f"{self._job_label(job)} · {len(outputs)} file(s)",
                        },
                    )
                    if outputs:
                        await event_bus.publish(
                            "asset.ready",
                            {
                                "job_id": job["id"],
                                "kind": job["kind"],
                                "paths": outputs,
                                "project_id": job["project_id"],
                                "message": f"Saved {self._job_label(job)}",
                            },
                        )
                except _CancelledByUser:
                    # The row already carries status='cancelled' (Stop button
                    # flipped it out-of-band). Do NOT mark_failed (that would
                    # bump retry_count and overwrite the terminal state).
                    # Emit job.failed with cancelled=True so the UI flips the
                    # node card and the user sees the stop took effect.
                    logger.info("Job %s cancelled by user", job["id"])
                    await event_bus.publish(
                        "job.failed",
                        {
                            "job_id": job["id"],
                            "kind": job["kind"],
                            "error": "cancelled by user",
                            "cancelled": True,
                            "project_id": job.get("project_id"),
                            "message": f"{self._job_label(job)} cancelled",
                        },
                    )
                except Exception as exc:
                    logger.exception("Job %s failed", job["id"])
                    queue_manager.mark_failed(job["id"], str(exc))
                    await event_bus.publish(
                        "job.failed",
                        {
                            "job_id": job["id"],
                            "kind": job["kind"],
                            "error": str(exc),
                            "project_id": job.get("project_id"),
                            "message": f"{self._job_label(job)} failed",
                        },
                    )
            except Exception:
                logger.exception("Worker loop error")
                await asyncio.sleep(config.settings.queue_poll_interval_sec)
        logger.info("Queue worker stopped")

    async def _run_job(self, job: dict[str, Any]) -> list[str]:
        payload = json.loads(job["payload_json"] or "{}")
        project_id = job["project_id"]
        kind = job["kind"]
        use_dry = bool(config.settings.dry_run)

        if kind == "export":
            # Export stitches local clips with ffmpeg — never touches ComfyUI,
            # and dry-run writes an mp4 placeholder (not the default PNG).
            return await run_export(job, payload, event_bus, dry_run=use_dry)

        client = ComfyUIClient(config.settings.comfyui_base_url)
        try:
            if use_dry:
                return await self._dry_run(job, payload)

            healthy = await client.health()
            if not healthy:
                raise RuntimeError(
                    f"ComfyUI unreachable at {config.settings.comfyui_base_url}. "
                    "Start ComfyUI, or enable Dry-run in Settings only for placeholder testing."
                )

            workflow_id = job.get("workflow_id") or payload.get("workflow_id")
            workflow = self._load_workflow(workflow_id)
            if not workflow:
                raise RuntimeError("No workflow found for job")

            input_values = payload.get("input_values") or {}
            patched = patch_workflow(workflow["nodes"], input_values)
            if workflow["strict_mode"]:
                patched = _apply_strict_mode(
                    patched, workflow["nodes"], workflow["schema"], input_values
                )
            patched = await client.prepare_media_inputs(patched)
            prompt_id = await client.queue_prompt(patched)

            history = await self._poll_history(client, prompt_id, job["id"])
            if not history:
                if queue_manager.is_cancelled(job["id"]):
                    raise _CancelledByUser()
                raise RuntimeError("Timed out waiting for ComfyUI history")

            status = history.get("status") or {}
            if status.get("status_str") == "error" or status.get("completed") is False:
                messages = status.get("messages") or []
                raise RuntimeError(f"ComfyUI error: {messages}")

            outputs_meta = select_output_files(
                client.extract_outputs(history), workflow["nodes"], kind
            )
            dest_dir = config.settings.assets_dir / str(project_id) / kind
            dest_dir.mkdir(parents=True, exist_ok=True)
            paths: list[str] = []
            for meta in outputs_meta:
                filename = meta["filename"]
                dest = dest_dir / filename
                await client.download_image(
                    filename,
                    subfolder=meta.get("subfolder", ""),
                    folder_type=meta.get("type", "output"),
                    dest=dest,
                )
                paths.append(str(dest))

            self._apply_outputs_to_entities(job, payload, paths)
            return paths
        finally:
            await client.close()

    async def _dry_run(self, job: dict[str, Any], payload: dict[str, Any]) -> list[str]:
        project_id = job["project_id"]
        kind = job["kind"]
        dest_dir = config.settings.assets_dir / str(project_id) / kind
        dest_dir.mkdir(parents=True, exist_ok=True)
        label = f"job-{job['id']}-{kind}"
        if kind == "video":
            path = write_placeholder_mp4(dest_dir / f"{label}.mp4", label=label)
        else:
            path = write_placeholder_png(dest_dir / f"{label}.png", label=label)
        paths = [str(path)]
        self._apply_outputs_to_entities(job, payload, paths)
        await asyncio.sleep(0.3)
        return paths

    async def _poll_history(
        self, client: ComfyUIClient, prompt_id: str, job_id: int | None = None
    ) -> dict[str, Any] | None:
        interval = max(config.settings.queue_poll_interval_sec, 0.5)
        timeout = config.settings.queue_poll_timeout_sec
        # 0 (or negative) = keep polling until the job completes or is cancelled.
        # Long video workflows routinely exceed 10 minutes, so the cap is now
        # configurable (default 30 min) instead of a hardcoded 600 seconds.
        attempts = None if timeout <= 0 else int(timeout / interval)
        n = 0
        while attempts is None or n < attempts:
            n += 1
            if self._stop.is_set():
                return None
            # Stop button: the job row was flipped to 'cancelled' out-of-band.
            # Interrupt the running ComfyUI prompt so the GPU stops NOW, then
            # bail — the row already carries the terminal state.
            if queue_manager.is_cancelled(job_id):
                await client.interrupt()
                return None
            history = await client.get_history(prompt_id)
            if history:
                return history
            await asyncio.sleep(interval)
            await event_bus.publish(
                "job.progress",
                {
                    "prompt_id": prompt_id,
                    "message": f"Waiting on ComfyUI ({prompt_id[:8]}…)",
                },
            )
        return None

    def _mark_project_completed(self, project_id: int) -> None:
        """A finished export closes the project lifecycle."""
        conn = get_db(config.settings.db_path)
        try:
            conn.execute(
                "UPDATE projects SET status = 'completed' WHERE id = ? AND status != 'completed'",
                (project_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def _load_workflow(self, workflow_id: int | None) -> dict[str, Any] | None:
        """The workflow's node graph plus what strict mode needs (schema + flag)."""
        if not workflow_id:
            return None
        conn = get_db(config.settings.db_path)
        try:
            row = conn.execute(
                "SELECT workflow_json, input_schema, strict_mode FROM workflows WHERE id = ?",
                (workflow_id,),
            ).fetchone()
            if not row:
                return None
            nodes = json.loads(row["workflow_json"])
            schema = (
                json.loads(row["input_schema"])
                if row["input_schema"]
                else parse_dynamic_inputs(nodes)
            )
            return {"nodes": nodes, "schema": schema, "strict_mode": bool(row["strict_mode"])}
        finally:
            conn.close()

    def _job_label(self, job: dict[str, Any]) -> str:
        payload = json.loads(job.get("payload_json") or "{}")
        kind = job.get("kind") or "job"
        if kind == "export":
            return "Export film"
        conn = get_db(config.settings.db_path)
        try:
            if payload.get("character_id"):
                row = conn.execute(
                    "SELECT name FROM characters WHERE id = ?", (payload["character_id"],)
                ).fetchone()
                name = row["name"] if row else f"#{payload['character_id']}"
                target = payload.get("asset_target") or "sheet"
                return f"{name} · {target}"
            if payload.get("location_id"):
                row = conn.execute(
                    "SELECT name FROM locations WHERE id = ?", (payload["location_id"],)
                ).fetchone()
                name = row["name"] if row else f"#{payload['location_id']}"
                return f"{name} · environment"
            if payload.get("item_id"):
                row = conn.execute(
                    "SELECT name FROM items WHERE id = ?", (payload["item_id"],)
                ).fetchone()
                name = row["name"] if row else f"#{payload['item_id']}"
                return f"{name} · item"
            if job.get("clip_id"):
                row = conn.execute(
                    """
                    SELECT s.heading, s.order_index AS s_pos, c.order_index AS c_pos
                    FROM clips c JOIN scenes s ON s.id = c.scene_id
                    WHERE c.id = ?
                    """,
                    (job["clip_id"],),
                ).fetchone()
                if row:
                    heading = (row["heading"] or f"Scene {row['s_pos']}").strip()
                    return f"#{row['s_pos']}.{row['c_pos']} · {heading}"
                return f"Clip #{job['clip_id']}"
            if job.get("scene_id"):
                row = conn.execute(
                    "SELECT heading, order_index FROM scenes WHERE id = ?", (job["scene_id"],)
                ).fetchone()
                if row:
                    heading = (row["heading"] or f"Scene {row['order_index']}").strip()
                    return f"Scene · {heading}"
                return f"Scene #{job['scene_id']}"
        finally:
            conn.close()
        return f"{kind} #{job.get('id')}"

    def _apply_outputs_to_entities(
        self, job: dict[str, Any], payload: dict[str, Any], paths: list[str]
    ) -> None:
        if not paths:
            return
        primary = paths[0]
        character_id = payload.get("character_id")
        location_id = payload.get("location_id")
        item_id = payload.get("item_id")
        scene_id = job.get("scene_id")
        conn = get_db(config.settings.db_path)
        try:
            if character_id:
                target = payload.get("asset_target") or "sheet"
                if target == "portrait":
                    # Legacy jobs only — UI no longer generates portraits
                    conn.execute(
                        "UPDATE characters SET portrait_path = ? WHERE id = ?",
                        (primary, character_id),
                    )
                else:
                    conn.execute(
                        "UPDATE characters SET sheet_path = ? WHERE id = ?",
                        (primary, character_id),
                    )
            if location_id:
                conn.execute(
                    "UPDATE locations SET reference_image_path = ? WHERE id = ?",
                    (primary, location_id),
                )
            if item_id:
                conn.execute(
                    "UPDATE items SET reference_image_path = ? WHERE id = ?",
                    (primary, item_id),
                )
            if job.get("clip_id") and job["kind"] == "video":
                conn.execute(
                    "UPDATE clips SET clip_path = ? WHERE id = ?",
                    (primary, job["clip_id"]),
                )
                # Mirror onto the scene for legacy readers (canvas scene cards,
                # old queries). The clip row stays the source of truth.
                conn.execute(
                    """
                    UPDATE scenes SET video_path = ? WHERE id = (
                        SELECT scene_id FROM clips WHERE id = ?
                    )
                    """,
                    (primary, job["clip_id"]),
                )
            elif scene_id and job["kind"] == "video":
                # Legacy job (pre-clips schema): write the scene's default clip.
                conn.execute(
                    """
                    UPDATE clips SET clip_path = ? WHERE id = (
                        SELECT c.id FROM clips c WHERE c.scene_id = ?
                        ORDER BY c.order_index, c.id LIMIT 1
                    )
                    """,
                    (primary, scene_id),
                )
                conn.execute(
                    "UPDATE scenes SET video_path = ? WHERE id = ?",
                    (primary, scene_id),
                )
            conn.commit()
        finally:
            conn.close()


queue_worker = QueueWorker()
