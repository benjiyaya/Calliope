"""Enqueue per-scene video generation jobs."""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from calliope.agent.llm import LLMClient
from calliope.agent.prompts import (
    build_minimax_h3_ref_messages,
    minimax_h3_ref_fallback,
    scene_video_prompt,
)
from calliope.comfyui.parser import parse_dynamic_inputs
from calliope.comfyui.roles import input_has_role
from calliope.comfyui.smart_fill import ref_image_slots, smart_fill_inputs
from calliope.config import settings
from calliope.db import get_db, row_to_dict
from calliope.events.bus import event_bus
from calliope.queue.manager import queue_manager

logger = logging.getLogger("calliope.video_agent")


def _h3_subjects(
    characters: list[dict[str, Any]],
    location: dict[str, Any] | None,
    loc_image: str | None,
    inputs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Ordered subject roster + ref image paths for the H3 ref profile.

    Order = ref wiring order: scene characters first (scene order), then the
    location. Capped at the workflow's generic (Input:image) slot count so
    <Subject N> always matches an actually-wired reference image.
    """
    subjects: list[dict[str, Any]] = []
    paths: list[str] = []
    for c in characters:
        img = c.get("sheet_path") or c.get("portrait_path")
        if not img:
            continue
        subjects.append(
            {
                "index": len(subjects) + 1,
                "kind": "character",
                "name": c.get("name"),
                "appearance": c.get("consistency_prompt") or c.get("appearance") or "",
            }
        )
        paths.append(img)
    if loc_image:
        loc = location or {}
        subjects.append(
            {
                "index": len(subjects) + 1,
                "kind": "location",
                "name": loc.get("name"),
                "appearance": loc.get("consistency_prompt") or loc.get("description") or "",
            }
        )
        paths.append(loc_image)
    cap = len(ref_image_slots(inputs))
    return subjects[:cap], paths[:cap]


async def _h3_rewrite(scene: dict[str, Any], subjects: list[dict[str, Any]]) -> str:
    """LLM rewrite into H3's six-section format, deterministic template on failure."""
    client = LLMClient.for_role("video")
    try:
        return await client.chat(
            build_minimax_h3_ref_messages(scene, subjects), temperature=0.4
        )
    except Exception as exc:
        logger.warning("MiniMax H3 prompt rewrite failed (%s); using fallback template", exc)
        return minimax_h3_ref_fallback(scene, subjects)
    finally:
        await client.close()


def _video_input(inputs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """First workflow input whose canonical role is ``video``."""
    for inp in inputs:
        if input_has_role(inp, "video"):
            return inp
    return None


def _previous_clip(
    conn: sqlite3.Connection,
    project_id: int,
    order_index: int,
) -> tuple[str | None, bool]:
    """Nearest earlier scene's clip path + whether any earlier scene exists.

    Returns ``(path, has_earlier_scene)``; path is None when the clip file does
    not exist on disk (not yet generated) or there is no earlier scene.
    """
    row = conn.execute(
        """
        SELECT video_path FROM scenes
        WHERE project_id = ? AND order_index < ? AND video_path IS NOT NULL
        ORDER BY order_index DESC LIMIT 1
        """,
        (project_id, order_index),
    ).fetchone()
    has_earlier = conn.execute(
        "SELECT 1 FROM scenes WHERE project_id = ? AND order_index < ? LIMIT 1",
        (project_id, order_index),
    ).fetchone() is not None
    path = row["video_path"] if row else None
    if path and Path(path).exists():
        return path, has_earlier
    return None, has_earlier


def _workflow_json(workflow: dict[str, Any] | None) -> dict[str, Any]:
    if not workflow:
        return {}
    raw = workflow.get("workflow_json")
    if isinstance(raw, str):
        return json.loads(raw)
    return raw or {}


def _get_workflow(workflow_id: int | None = None) -> dict[str, Any] | None:
    conn = get_db(settings.db_path)
    try:
        if workflow_id:
            row = conn.execute(
                "SELECT * FROM workflows WHERE id = ? AND is_enabled = 1", (workflow_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM workflows WHERE kind = 'video' AND is_enabled = 1 ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT * FROM workflows WHERE is_enabled = 1 ORDER BY id ASC LIMIT 1"
                ).fetchone()
        return row_to_dict(row) if row else None
    finally:
        conn.close()


async def enqueue_video_jobs(
    project_id: int,
    *,
    scene_ids: list[int] | None = None,
    workflow_id: int | None = None,
    input_values_override: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    await event_bus.publish(
        "agent.thinking", {"message": "Queuing video jobs…", "project_id": project_id}
    )
    conn = get_db(settings.db_path)
    jobs: list[dict[str, Any]] = []
    try:
        query = "SELECT * FROM scenes WHERE project_id = ?"
        params: list[Any] = [project_id]
        if scene_ids:
            placeholders = ",".join("?" * len(scene_ids))
            query += f" AND id IN ({placeholders})"
            params.extend(scene_ids)
        query += " ORDER BY order_index"
        scenes = [row_to_dict(r) for r in conn.execute(query, params).fetchall()]

        for scene in scenes:
            # Supersede leftover pending jobs so a re-Generate always starts fresh
            conn.execute(
                """
                UPDATE jobs SET status = 'failed', error = 'superseded by new generate',
                completed_at = CURRENT_TIMESTAMP
                WHERE scene_id = ? AND kind = 'video' AND status = 'pending'
                """,
                (scene["id"],),
            )
            # Clear prior clip so UI shows generating instead of the old video
            conn.execute(
                "UPDATE scenes SET video_path = NULL WHERE id = ?",
                (scene["id"],),
            )
            # Commit BEFORE enqueue: queue_manager.enqueue writes on its own
            # connection, and holding this transaction open across that call
            # self-deadlocks (sqlite3.OperationalError: database is locked).
            conn.commit()

            wf_id = workflow_id or scene.get("workflow_id")
            workflow = _get_workflow(wf_id)

            workflow_json = _workflow_json(workflow)
            inputs = parse_dynamic_inputs(workflow_json) if workflow_json else []
            duration = scene.get("duration_sec")

            char_rows = conn.execute(
                """
                SELECT c.* FROM characters c
                JOIN scene_characters sc ON sc.character_id = c.id
                WHERE sc.scene_id = ?
                """,
                (scene["id"],),
            ).fetchall()
            characters = [row_to_dict(r) for r in char_rows]
            char_image = next(
                (c.get("sheet_path") or c.get("portrait_path") for c in characters if c.get("sheet_path") or c.get("portrait_path")),
                None,
            )
            loc_image = scene.get("env_image_path")
            loc_row: dict[str, Any] | None = None
            if scene.get("location_id"):
                row = conn.execute(
                    "SELECT name, description, consistency_prompt, reference_image_path "
                    "FROM locations WHERE id = ?",
                    (scene["location_id"],),
                ).fetchone()
                if row:
                    loc_row = row_to_dict(row)
                    if not loc_image:
                        loc_image = loc_row["reference_image_path"]

            profile = (workflow or {}).get("prompt_profile") or "prose"
            if profile == "minimax_h3_ref":
                subjects, ref_paths = _h3_subjects(characters, loc_row, loc_image, inputs)
                await event_bus.publish(
                    "agent.thinking",
                    {
                        "message": f"H3 prompt rewrite · scene {scene.get('order_index')}",
                        "project_id": project_id,
                    },
                )
                prompt = await _h3_rewrite(scene, subjects)
                values = smart_fill_inputs(
                    inputs,
                    prompt=prompt,
                    ref_images=ref_paths,
                    duration=duration,
                    extra=input_values_override,
                )
            else:
                prompt = scene_video_prompt(scene, characters)
                values = smart_fill_inputs(
                    inputs,
                    prompt=prompt,
                    character_image=char_image,
                    location_image=loc_image,
                    duration=duration,
                    extra=input_values_override,
                )
            payload: dict[str, Any] = {"input_values": values, "prompt": prompt}
            if scene.get("chain_from_prev"):
                video_input = _video_input(inputs)
                if not video_input:
                    raise ValueError(
                        f"Scene {scene.get('order_index')} is marked continue-from-previous "
                        f"but workflow '{(workflow or {}).get('name') or workflow_id}' has no "
                        "video input — pick a workflow with a (Input:video) node."
                    )
                video_node_id = str(video_input["nodeId"])
                if not values.get(video_node_id):
                    # Explicit clip from the form / input_values_override wins.
                    prev_clip, has_earlier = _previous_clip(
                        conn, project_id, scene.get("order_index") or 0
                    )
                    if prev_clip:
                        # Local path: the worker's prepare_media_inputs uploads it
                        # to ComfyUI before queuing (same shape as char/loc refs).
                        values[video_node_id] = prev_clip
                    elif has_earlier:
                        # Previous clip not generated yet (typical when a batch is
                        # queued in one go). The worker resolves it at RUN time —
                        # the queue is concurrency-1, so the earlier scene's clip
                        # will exist by then.
                        payload["continue_source"] = {
                            "scene_order_index": scene.get("order_index"),
                        }
                    else:
                        raise ValueError(
                            f"Scene {scene.get('order_index')} is marked continue-from-previous "
                            "but no previous clip exists yet — generate an earlier clip first "
                            "or upload a video in the Video stage."
                        )
            job = queue_manager.enqueue(
                project_id=project_id,
                kind="video",
                workflow_id=workflow["id"] if workflow else None,
                scene_id=scene["id"],
                payload=payload,
            )
            if workflow and not scene.get("workflow_id"):
                conn.execute(
                    "UPDATE scenes SET workflow_id = ? WHERE id = ?",
                    (workflow["id"], scene["id"]),
                )
            # Never hold a write lock across awaits (event publish below).
            conn.commit()
            jobs.append(job)
            await event_bus.publish(
                "job.created",
                {
                    "job_id": job["id"],
                    "kind": "video",
                    "message": f"Scene · {scene.get('heading') or scene.get('order_index')}",
                    "project_id": project_id,
                },
            )
        conn.commit()
    finally:
        conn.close()
    return jobs
