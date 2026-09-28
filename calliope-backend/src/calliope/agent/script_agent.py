"""Generate scene scripts via LLM and persist them."""
from __future__ import annotations

from typing import Any

from calliope.agent.llm import generate_structured
from calliope.agent.prompts import (
    build_script_chunk_messages,
    estimate_scene_duration_sec,
    estimate_target_seconds,
    recommend_scene_count,
)
from calliope.config import settings
from calliope.db import ensure_default_clip, get_db, row_to_dict
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


async def _iter_scene_chunks(
    *,
    project_id: int,
    p: dict[str, Any],
    beats: list[dict[str, Any]],
    characters: list[dict[str, Any]],
    locations: list[dict[str, Any]],
    required_scenes: int,
):
    """Yield one list of scenes per chunk as each LLM call completes.

    Yields promptly so `generate_script` can persist + commit chunk by chunk —
    a crash mid-generation keeps every already-committed chunk instead of
    losing the whole script. Raises at the end if the board came up short.
    """
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
        yield scenes

    if len(collected) < required_scenes:
        raise ValueError(
            f"Script returned {len(collected)} scenes but {required_scenes} were required. "
            "Add scenes again and retry, or raise target duration."
        )


async def _generate_scenes_chunked(
    *,
    project_id: int,
    p: dict[str, Any],
    beats: list[dict[str, Any]],
    characters: list[dict[str, Any]],
    locations: list[dict[str, Any]],
    required_scenes: int,
) -> list[dict[str, Any]]:
    """Whole-board convenience wrapper over `_iter_scene_chunks`."""
    collected: list[dict[str, Any]] = []
    async for scenes in _iter_scene_chunks(
        project_id=project_id,
        p=p,
        beats=beats,
        characters=characters,
        locations=locations,
        required_scenes=required_scenes,
    ):
        collected.extend(scenes)
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
        ensure_default_clip(conn, scene_id, project_id)
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


def _script_board(conn, project_id: int, scene_count: int | None) -> dict[str, Any]:
    """Project, story and the required scene count — shared by the LLM and the brief."""
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not project:
        raise ValueError("Project not found")
    p = row_to_dict(project)

    def rows(sql: str) -> list[dict[str, Any]]:
        return [row_to_dict(r) for r in conn.execute(sql, (project_id,)).fetchall()]

    existing_n = conn.execute(
        "SELECT COUNT(*) AS n FROM scenes WHERE project_id = ?", (project_id,)
    ).fetchone()["n"]
    recommended = recommend_scene_count(p.get("target_duration"))
    requested = scene_count if scene_count and scene_count > 0 else existing_n
    required = max(recommended, int(requested)) if requested else recommended
    return {
        "p": p,
        "beats": rows("SELECT * FROM story_beats WHERE project_id = ? ORDER BY order_index"),
        "characters": rows("SELECT * FROM characters WHERE project_id = ?"),
        "locations": rows("SELECT * FROM locations WHERE project_id = ?"),
        "existing_n": existing_n,
        "recommended": recommended,
        "required": required,
    }


def script_brief(project_id: int, scene_count: int | None = None) -> dict[str, Any]:
    """What the script model would receive — for an MCP client to write the script."""
    from calliope.agent.prompts import DEFAULT_CLIP_DURATION_SEC

    conn = get_db(settings.db_path)
    try:
        board = _script_board(conn, project_id, scene_count)
    finally:
        conn.close()
    required = board["required"]
    messages = build_script_chunk_messages(
        title=board["p"]["title"],
        idea=board["p"].get("idea"),
        beats=board["beats"],
        characters=board["characters"],
        locations=board["locations"],
        target_duration=board["p"].get("target_duration"),
        scene_count=required,
        chunk_start=1,
        chunk_scenes=required,
        previous_tail=[],
    )
    return {
        "system": messages[0]["content"],
        "user": messages[1]["content"],
        "required_scenes": required,
        "characters": [{"id": c["id"], "name": c["name"]} for c in board["characters"]],
        "locations": [{"id": loc["id"], "name": loc["name"]} for loc in board["locations"]],
        "clips": (
            "Optional per scene: \"clips\": [{description, shot_size (wide|medium|closeUp|"
            "insert|overShoulder), duration_sec (3-"
            f"{DEFAULT_CLIP_DURATION_SEC}), dialog_lines_covered: [1-based line numbers of the "
            "scene dialog], chain_from_prev}] — every dialogue line in exactly one clip. "
            "Scenes without clips keep one default clip (break_into_shots can split them later)."
        ),
    }


async def generate_script(
    project_id: int,
    *,
    replace: bool = True,
    scene_count: int | None = None,
    with_clips: bool = True,
    content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the script with the LLM, or save ``content`` from an MCP client.

    Client content — {"scenes": [{heading, action, dialog, location_id,
    character_ids, clips?}]} — goes through the same persistence; it is
    validated before anything is deleted and never calls the LLM (scenes
    that bring their own clips are split with them, the rest keep their
    default clip).
    """
    await event_bus.publish(
        "agent.thinking", {"message": "Writing scene script…", "project_id": project_id}
    )
    conn = get_db(settings.db_path)
    try:
        # Prefer explicit request, else keep at least the board the user already built
        board = _script_board(conn, project_id, scene_count)
        p, beats = board["p"], board["beats"]
        characters, locations = board["characters"], board["locations"]
        existing_n, recommended = board["existing_n"], board["recommended"]
        required_scenes = board["required"]
        client_scenes: list[dict[str, Any]] | None = None
        if content is not None:
            if not isinstance(content, dict):
                raise ValueError("content must be a JSON object: {\"scenes\": [...]}")
            client_scenes = [dict(s) for s in content.get("scenes") or [] if isinstance(s, dict)]
            if len(client_scenes) < required_scenes:
                raise ValueError(
                    f"content has {len(client_scenes)} scenes but {required_scenes} are required "
                    f"(target duration suggests {recommended}; the board had {existing_n})."
                )
            for index, scene in enumerate(client_scenes, start=1):
                scene["order_index"] = index

        await event_bus.publish(
            "agent.thinking",
            {
                "message": f"Writing {required_scenes} scenes "
                f"(board had {existing_n}; duration suggests {recommended})…",
                "project_id": project_id,
            },
        )
        target_seconds = estimate_target_seconds(p.get("target_duration"))

        # Chunked generation: one LLM call per SCRIPT_CHUNK scenes. A single
        # 20+ scene request means a multi-thousand-token JSON answer that
        # small/local models take minutes to stream (or fail outright and
        # trigger a full-script retry) — the "Regenerate Script hangs" bug.
        # Small boards (one chunk) keep the exact single-call behavior.
        created: list[dict[str, Any]] = []
        first_chunk = True

        async def _client_chunks():
            yield client_scenes

        chunks = (
            _client_chunks()
            if client_scenes is not None
            else _iter_scene_chunks(
                project_id=project_id,
                p=p,
                beats=beats,
                characters=characters,
                locations=locations,
                required_scenes=required_scenes,
            )
        )
        async for scenes in chunks:
            if replace and first_chunk:
                # Clear the old board only once the first replacement chunk is
                # in hand — a failed first chunk leaves the old script intact.
                scene_ids = [
                    r["id"]
                    for r in conn.execute(
                        "SELECT id FROM scenes WHERE project_id = ?", (project_id,)
                    ).fetchall()
                ]
                for sid in scene_ids:
                    conn.execute("DELETE FROM scene_characters WHERE scene_id = ?", (sid,))
                conn.execute("DELETE FROM scenes WHERE project_id = ?", (project_id,))
                first_chunk = False
            created.extend(_persist_scenes(conn, project_id, scenes))
            # Commit per chunk: every finished chunk is durable, so a crash,
            # stop, or disconnect mid-generation keeps what was already paid
            # for instead of losing the whole script in one failed transaction.
            conn.commit()
            await event_bus.publish(
                "agent.thinking",
                {
                    "message": f"Saved {len(created)}/{required_scenes} scenes…",
                    "project_id": project_id,
                },
            )

        # Durations are OUR estimate from actual content, not the model's flat
        # guess — and the user's target runtime is the total they sum to. Runs
        # AFTER all chunks are committed, as a bulk UPDATE over the durable
        # board (the rescale is global; the rows were persisted per chunk).
        # Issue #64: the scale may go BELOW 1.0 (floor 0.5) so an over-written
        # script's scene budgets shrink toward the target too — the old
        # max(..., 1.0) treated the target as a floor, and a 30s ask produced a
        # 1:51 script. The floor keeps a wildly over-written board from being
        # mislabeled more than 2x; content itself is not edited.
        if target_seconds:
            all_rows = conn.execute(
                "SELECT id, action, dialog FROM scenes WHERE project_id = ? ORDER BY order_index",
                (project_id,),
            ).fetchall()
            estimates = [
                estimate_scene_duration_sec({"action": r["action"], "dialog": r["dialog"]})
                for r in all_rows
            ]
            total_est = sum(estimates)
            if total_est > 0:
                scale = max(target_seconds / total_est, 0.5)
                for row, est in zip(all_rows, estimates):
                    conn.execute(
                        "UPDATE scenes SET duration_sec = ? WHERE id = ?",
                        (max(3, round(est * scale)), row["id"]),
                    )

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

        # Break into shots: expand every fresh scene into renderable shot
        # clips as part of the SAME tool call. `expand_scene_coverage`
        # persists per scene (one commit per scene), so this stage is
        # durable too — a crash keeps the script plus every clip already
        # expanded. The default clip from `_persist_scenes` is what gets
        # replaced here, so nothing is lost if this stage is interrupted.
        clips_summary: list[dict[str, Any]] = []
        without_clips: list[int] = []
        if client_scenes is not None:
            # Client content never calls the coverage LLM: split the scenes
            # that brought their clips, report the ones that did not.
            provided = {
                int(row["id"]): scene.get("clips")
                for row, scene in zip(created, client_scenes)
                if scene.get("clips")
            }
            without_clips = [int(r["order_index"]) for r in created if int(r["id"]) not in provided]
            if provided:
                from calliope.agent.coverage_agent import expand_scene_coverage

                coverage = await expand_scene_coverage(
                    project_id, list(provided), provided=provided
                )
                clips_summary = coverage.get("scenes") or []
        elif with_clips and created:
            await event_bus.publish(
                "agent.thinking",
                {
                    "message": f"Breaking {len(created)} scenes into shot clips…",
                    "project_id": project_id,
                },
            )
            from calliope.agent.coverage_agent import expand_scene_coverage

            coverage = await expand_scene_coverage(
                project_id,
                [int(s["id"]) for s in created],
            )
            clips_summary = coverage.get("scenes") or []
            total_clips = sum(int(r.get("clips") or 0) for r in clips_summary)
            await event_bus.publish(
                "agent.thinking",
                {
                    "message": f"Shot list done — {total_clips} clips across "
                    f"{len(clips_summary)} scenes",
                    "project_id": project_id,
                },
            )

        result = {
            "ok": True,
            "scenes": created,
            "clips": clips_summary,
            "generated": {"scenes": created},
        }
        if without_clips:
            result["scenes_without_clips"] = without_clips
            result["note"] = (
                "These scenes kept one default clip; split them with break_into_shots "
                "(content) if they need coverage."
            )
        return result
    finally:
        conn.close()
