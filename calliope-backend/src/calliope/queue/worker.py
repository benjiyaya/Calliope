"""Background queue worker loop."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pathlib import Path as _fs_path

from calliope import config
from calliope.comfyui.client import ComfyUIClient
from calliope.comfyui.dry_run import write_placeholder_mp4, write_placeholder_png
from calliope.comfyui.parser import parse_dynamic_inputs
from calliope.comfyui.patcher import patch_workflow
from calliope.comfyui.roles import input_has_role
from calliope.db import get_db
from calliope.events.bus import event_bus
from calliope.export.runner import run_export
from calliope.queue.manager import queue_manager

logger = logging.getLogger("calliope.worker")


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
                    await event_bus.publish(
                        "job.completed",
                        {
                            "job_id": job["id"],
                            "kind": job["kind"],
                            "outputs": outputs,
                            "project_id": job["project_id"],
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
            if payload.get("continue_source") and kind == "video":
                input_values = await self._resolve_continue_source(
                    job, payload, workflow, dict(input_values)
                )
            patched = patch_workflow(workflow, input_values)
            patched = await client.prepare_media_inputs(patched)
            prompt_id = await client.queue_prompt(patched)

            history = await self._poll_history(client, prompt_id)
            if not history:
                raise RuntimeError("Timed out waiting for ComfyUI history")

            status = history.get("status") or {}
            if status.get("status_str") == "error" or status.get("completed") is False:
                messages = status.get("messages") or []
                raise RuntimeError(f"ComfyUI error: {messages}")

            outputs_meta = client.extract_outputs(history)
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

    async def _resolve_continue_source(
        self,
        job: dict[str, Any],
        payload: dict[str, Any],
        workflow: dict[str, Any],
        input_values: dict[str, Any],
    ) -> dict[str, Any]:
        """Fill the workflow's video input with the previous scene's clip.

        Enqueue defers continue-scenes whose earlier clip does not exist yet
        (batch generation); the queue is concurrency-1, so by the time this job
        runs the earlier clip has been rendered and attached to its scene. The
        path is injected into ``input_values`` like any user-provided value —
        ``patch_workflow`` writes it onto the ``(Input:video)`` node and
        ``prepare_media_inputs`` uploads it to ComfyUI before queuing.
        """
        project_id = job["project_id"]
        order_index = (payload.get("continue_source") or {}).get("scene_order_index")
        if order_index is None:
            order_index = payload.get("scene_order_index") or 0

        prev_order: int | None = None
        prev_clip: str | None = None
        conn = get_db(config.settings.db_path)
        try:
            row = conn.execute(
                """
                SELECT order_index, video_path FROM scenes
                WHERE project_id = ? AND order_index < ?
                ORDER BY order_index DESC LIMIT 1
                """,
                (project_id, order_index),
            ).fetchone()
            if row:
                prev_order = row["order_index"]
                prev_clip = row["video_path"]
        finally:
            conn.close()

        scene_n = order_index
        if prev_order is None:
            raise RuntimeError(
                f"Continue scene {scene_n}: no earlier scene in this project to continue from."
            )
        if not prev_clip or not _fs_path(prev_clip).exists():
            raise RuntimeError(
                f"Continue scene {scene_n}: previous clip (scene {prev_order}) has no video file "
                "— generate the earlier clip first."
            )

        video_node: str | None = None
        for inp in parse_dynamic_inputs(workflow):
            if input_has_role(inp, "video"):
                video_node = str(inp["nodeId"])
                break
        if video_node is None:
            raise RuntimeError(
                "Continue scene "
                f"{scene_n}: workflow has no (Input:video) node to receive the previous clip."
            )

        input_values[video_node] = prev_clip
        await event_bus.publish(
            "job.progress",
            {
                "job_id": job["id"],
                "project_id": project_id,
                "message": f"Continuing from scene {prev_order} clip",
            },
        )
        return input_values

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

    async def _poll_history(self, client: ComfyUIClient, prompt_id: str) -> dict[str, Any] | None:
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
        if not workflow_id:
            return None
        conn = get_db(config.settings.db_path)
        try:
            row = conn.execute(
                "SELECT workflow_json FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if not row:
                return None
            return json.loads(row["workflow_json"])
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
            if scene_id and job["kind"] == "video":
                conn.execute(
                    "UPDATE scenes SET video_path = ? WHERE id = ?",
                    (primary, scene_id),
                )
            conn.commit()
        finally:
            conn.close()


queue_worker = QueueWorker()
