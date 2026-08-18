"""Enqueue character/location image generation jobs."""
from __future__ import annotations

import json
from typing import Any

from calliope.agent.prompts import character_image_prompt, location_image_prompt
from calliope.comfyui.parser import parse_dynamic_inputs
from calliope.comfyui.smart_fill import smart_fill_inputs
from calliope.config import settings
from calliope.db import get_db, row_to_dict
from calliope.events.bus import event_bus
from calliope.queue.manager import queue_manager


def _has_text_inputs(inputs: list[dict[str, Any]]) -> bool:
    return any(inp.get("kind") in ("text", "textarea") for inp in inputs)


def _sanitize_input_overrides(extra: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop null/blank overrides so empty shared Text Prompt cannot wipe the entity prompt."""
    if not extra:
        return None
    cleaned: dict[str, Any] = {}
    for k, v in extra.items():
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        cleaned[str(k)] = v
    return cleaned or None


def _get_enabled_workflow(kind: str = "image", workflow_id: int | None = None) -> dict[str, Any] | None:
    conn = get_db(settings.db_path)
    try:
        if workflow_id:
            row = conn.execute(
                "SELECT * FROM workflows WHERE id = ? AND is_enabled = 1", (workflow_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM workflows WHERE kind = ? AND is_enabled = 1 ORDER BY id ASC LIMIT 1",
                (kind,),
            ).fetchone()
        return row_to_dict(row) if row else None
    finally:
        conn.close()


async def enqueue_asset_jobs(
    project_id: int,
    *,
    character_ids: list[int] | None = None,
    location_ids: list[int] | None = None,
    missing_only: bool = True,
    workflow_id: int | None = None,
    input_values_override: dict[str, Any] | None = None,
    asset_target: str = "sheet",
    prompt_override: str | None = None,
) -> list[dict[str, Any]]:
    await event_bus.publish(
        "agent.thinking",
        {"message": "Preparing asset generation from saved image prompts…", "project_id": project_id},
    )
    workflow = _get_enabled_workflow("image", workflow_id)
    workflow_json = json.loads(workflow["workflow_json"]) if workflow else {}
    inputs = parse_dynamic_inputs(workflow_json) if workflow_json else []
    target = asset_target if asset_target in {"sheet", "portrait"} else "sheet"
    input_values_override = _sanitize_input_overrides(input_values_override)
    has_override = bool(input_values_override)
    allow_empty_prompt = has_override or not _has_text_inputs(inputs)

    conn = get_db(settings.db_path)
    jobs: list[dict[str, Any]] = []
    skipped: list[str] = []
    try:
        chars = conn.execute(
            "SELECT * FROM characters WHERE project_id = ?", (project_id,)
        ).fetchall()
        locs = conn.execute(
            "SELECT * FROM locations WHERE project_id = ?", (project_id,)
        ).fetchall()

        for row in chars:
            c = row_to_dict(row)
            if character_ids is not None and c["id"] not in character_ids:
                continue
            existing = c.get("sheet_path") if target == "sheet" else c.get("portrait_path")
            if missing_only and existing:
                continue
            prompt = (prompt_override or "").strip() or character_image_prompt(c, kind=target)
            if not prompt.strip() and not allow_empty_prompt:
                skipped.append(c.get("name") or str(c["id"]))
                continue
            values = smart_fill_inputs(
                inputs,
                prompt=prompt or None,
                extra=input_values_override,
            )
            job = queue_manager.enqueue(
                project_id=project_id,
                kind="image",
                workflow_id=workflow["id"] if workflow else None,
                payload={
                    "input_values": values,
                    "character_id": c["id"],
                    "asset_target": target,
                    "prompt": prompt,
                },
            )
            jobs.append(job)
            await event_bus.publish(
                "job.created",
                {
                    "job_id": job["id"],
                    "kind": "image",
                    "message": f"{c.get('name')} · {target}",
                },
            )

        # Only enqueue locations when not doing a character-only generate
        enqueue_locs = location_ids is not None or character_ids is None
        if enqueue_locs:
            for row in locs:
                loc = row_to_dict(row)
                if location_ids is not None and loc["id"] not in location_ids:
                    continue
                if character_ids is not None and location_ids is None:
                    # Character-scoped call — skip locations
                    continue
                if missing_only and loc.get("reference_image_path"):
                    continue
                prompt = (prompt_override or "").strip() or location_image_prompt(loc)
                if not prompt.strip() and not allow_empty_prompt:
                    skipped.append(loc.get("name") or str(loc["id"]))
                    continue
                values = smart_fill_inputs(
                    inputs,
                    prompt=prompt or None,
                    extra=input_values_override,
                )
                job = queue_manager.enqueue(
                    project_id=project_id,
                    kind="image",
                    workflow_id=workflow["id"] if workflow else None,
                    payload={
                        "input_values": values,
                        "location_id": loc["id"],
                        "prompt": prompt,
                    },
                )
                jobs.append(job)
                await event_bus.publish(
                    "job.created",
                    {
                        "job_id": job["id"],
                        "kind": "image",
                        "message": f"{loc.get('name')} · environment",
                    },
                )
    finally:
        conn.close()

    if skipped:
        await event_bus.publish(
            "agent.thinking",
            {
                "project_id": project_id,
                "message": f"Skipped (empty image prompt): {', '.join(skipped)}",
            },
        )
    return jobs
