"""Generate scene scripts via LLM and persist them."""
from __future__ import annotations

from typing import Any

from calliope.agent.llm import generate_structured
from calliope.agent.prompts import build_script_chunk_messages, recommend_scene_count
from calliope.config import settings
from calliope.db import get_db, row_to_dict
from calliope.events.bus import event_bus

# Scenes per LLM call. A single 20-scene request means a multi-thousand-token
# JSON answer that small/local models take minutes to stream (or truncate and
# force a full-script retry) — the "Regenerate Script hangs" bug. Chunks keep
# each call modest and let us publish progress between them.
SCRIPT_CHUNK = 4


async def _request_chunk(
    *,
    p: dict[str, Any],
    beats: list[dict[str, Any]],
    characters: list[dict[str, Any]],
    locations: list[dict[str, Any]],
    required_scenes: int,
    chunk_start: int,
    chunk_scenes: int,
    previous_tail: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    messages = build_script_chunk_messages(
        title=p["title"],
        idea=p.get("idea"),
        beats=beats,
        characters=characters,
        locations=locations,
        target_duration=p.get("target_duration"),
        scene_count=required_scenes,
        chunk_start=chunk_start,
        chunk_scenes=chunk_scenes,
        previous_tail=previous_tail,
    )
    result = await generate_structured(messages, temperature=0.7)
    scenes = result.get("scenes") or []
    if len(scenes) < chunk_scenes:
        # Retry only this chunk — the old code retried the ENTIRE script.
        retry_messages = [
            messages[0],
            {
                "role": "user",
                "content": messages[1]["content"]
                + (
                    f"\n\nPREVIOUS ATTEMPT FAILED: it only had {len(scenes)} scenes. "
                    f"You MUST return exactly {chunk_scenes} scenes this time."
                ),
            },
        ]
        result = await generate_structured(retry_messages, temperature=0.5)
        scenes = result.get("scenes") or []
    return scenes[:chunk_scenes]


async def _generate_scenes_chunked(
    *,
    project_id: int,
    p: dict[str, Any],
    beats: list[dict[str, Any]],
    characters: list[dict[str, Any]],
    locations: list[dict[str, Any]],
    required_scenes: int,
) -> list[dict[str, Any]]:
    plan: list[tuple[int, int]] = []
    start = 1
    while start <= required_scenes:
        n = min(SCRIPT_CHUNK, required_scenes - start + 1)
        plan.append((start, n))
        start += n

    collected: list[dict[str, Any]] = []
    total = len(plan)
    for idx, (chunk_start, chunk_scenes) in enumerate(plan, start=1):
        if total > 1:
            await event_bus.publish(
                "agent.thinking",
                {
                    "message": f"Writing scenes {chunk_start}–{chunk_start + chunk_scenes - 1} "
                    f"(chunk {idx}/{total})…",
                    "project_id": project_id,
                },
            )
        scenes = await _request_chunk(
            p=p,
            beats=beats,
            characters=characters,
            locations=locations,
            required_scenes=required_scenes,
            chunk_start=chunk_start,
            chunk_scenes=chunk_scenes,
            previous_tail=collected[-2:],
        )
        # Models sometimes restart order_index per chunk; renumber by offset
        # so the persisted board is always 1..N in write order.
        for offset, scene in enumerate(scenes):
            scene["order_index"] = chunk_start + offset
        collected.extend(scenes)

    if len(collected) < required_scenes:
        raise ValueError(
            f"Script returned {len(collected)} scenes but {required_scenes} were required. "
            "Add scenes again and retry, or raise target duration."
        )
    return collected[:required_scenes]


def _persist_scenes(
    conn,
    project_id: int,
    scenes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    created: list[dict[str, Any]] = []
    for scene in scenes:
        loc_id = scene.get("location_id")
        env_path = None
        if loc_id:
            loc = conn.execute(
                "SELECT reference_image_path FROM locations WHERE id = ? AND project_id = ?",
                (loc_id, project_id),
            ).fetchone()
            if loc:
                env_path = loc["reference_image_path"]

        cur = conn.execute(
            """
            INSERT INTO scenes
            (project_id, order_index, heading, action, dialog, duration_sec, location_id, env_image_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                scene.get("order_index", 0),
                scene.get("heading"),
                scene.get("action"),
                scene.get("dialog"),
                scene.get("duration_sec"),
                loc_id,
                env_path,
            ),
        )
        scene_id = cur.lastrowid
        for cid in scene.get("character_ids") or []:
            exists = conn.execute(
                "SELECT id FROM characters WHERE id = ? AND project_id = ?",
                (cid, project_id),
            ).fetchone()
            if exists:
                conn.execute(
                    "INSERT OR IGNORE INTO scene_characters (scene_id, character_id) VALUES (?, ?)",
                    (scene_id, cid),
                )
        row = conn.execute("SELECT * FROM scenes WHERE id = ?", (scene_id,)).fetchone()
        created.append(row_to_dict(row))
    return created


async def generate_script(
    project_id: int,
    *,
    replace: bool = True,
    scene_count: int | None = None,
) -> dict[str, Any]:
    await event_bus.publish(
        "agent.thinking", {"message": "Writing scene script…", "project_id": project_id}
    )
    conn = get_db(settings.db_path)
    try:
        project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not project:
            raise ValueError("Project not found")
        p = row_to_dict(project)
        beats = [
            row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM story_beats WHERE project_id = ? ORDER BY order_index",
                (project_id,),
            ).fetchall()
        ]
        characters = [
            row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM characters WHERE project_id = ?", (project_id,)
            ).fetchall()
        ]
        locations = [
            row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM locations WHERE project_id = ?", (project_id,)
            ).fetchall()
        ]

        existing_n = conn.execute(
            "SELECT COUNT(*) AS n FROM scenes WHERE project_id = ?",
            (project_id,),
        ).fetchone()["n"]
        recommended = recommend_scene_count(p.get("target_duration"))
        # Prefer explicit request, else keep at least the board the user already built
        requested = scene_count if scene_count and scene_count > 0 else existing_n
        required_scenes = max(recommended, int(requested)) if requested else recommended

        await event_bus.publish(
            "agent.thinking",
            {
                "message": f"Writing {required_scenes} scenes "
                f"(board had {existing_n}; duration suggests {recommended})…",
                "project_id": project_id,
            },
        )

        # Chunked generation: one LLM call per SCRIPT_CHUNK scenes. A single
        # 20+ scene request means a multi-thousand-token JSON answer that
        # small/local models take minutes to stream (or fail outright and
        # trigger a full-script retry) — the "Regenerate Script hangs" bug.
        # Small boards (one chunk) keep the exact single-call behavior.
        scenes_out = await _generate_scenes_chunked(
            project_id=project_id,
            p=p,
            beats=beats,
            characters=characters,
            locations=locations,
            required_scenes=required_scenes,
        )

        if replace:
            scene_ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM scenes WHERE project_id = ?", (project_id,)
                ).fetchall()
            ]
            for sid in scene_ids:
                conn.execute("DELETE FROM scene_characters WHERE scene_id = ?", (sid,))
            conn.execute("DELETE FROM scenes WHERE project_id = ?", (project_id,))

        created = _persist_scenes(conn, project_id, scenes_out)

        conn.execute(
            "UPDATE projects SET status = 'in_progress', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (project_id,),
        )
        conn.commit()
        await event_bus.publish(
            "agent.thinking",
            {
                "message": f"Script written — {len(created)} scenes",
                "project_id": project_id,
            },
        )
        return {"ok": True, "scenes": created, "generated": {"scenes": scenes_out}}
    finally:
        conn.close()
