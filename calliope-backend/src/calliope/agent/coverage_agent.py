"""Break a script scene into N renderable shot clips (coverage expansion).

One script scene usually films as many shots: establishing wide, dialogue
coverage, cutaways, insert close-ups. This pass asks the LLM to allocate the
scene's full content — every dialogue line and significant action beat —
across those clips, with durations that sum to the scene's budget and each
clip inside the per-clip duration cap.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from calliope.agent.llm import generate_structured
from calliope.agent.prompts import DEFAULT_CLIP_DURATION_SEC, estimate_scene_duration_sec
from calliope.config import settings
from calliope.db import get_db, row_to_dict
from calliope.events.bus import event_bus

logger = logging.getLogger("calliope.coverage_agent")

# Scenes expanded per LLM call (same chunking rationale as generate_script).
COVERAGE_CHUNK = 4


def _dialog_lines(dialog: str | None) -> list[str]:
    return [ln for ln in (dialog or "").splitlines() if ln.strip()]


def _coverage_messages(
    *,
    scene: dict[str, Any],
    characters: list[dict[str, Any]],
    previous_heading: str | None,
    next_heading: str | None,
    clip_cap: int,
) -> list[dict[str, str]]:
    """LLM messages that split one scene into shot clips."""
    lines = _dialog_lines(scene.get("dialog"))
    numbered_dialog = (
        "\n".join(f"{i + 1}. {ln}" for i, ln in enumerate(lines))
        or "(no dialogue in this scene)"
    )
    char_lines = "\n".join(
        f"- id={c['id']} {c['name']}: {(c.get('appearance') or '').strip()}"
        for c in characters
    )
    scene_secs = estimate_scene_duration_sec(scene) or scene.get("duration_sec") or clip_cap
    n_clips_hint = max(1, round(scene_secs / clip_cap))
    context = ""
    if previous_heading or next_heading:
        context = (
            f"\nNeighbor scenes (for continuity, do NOT rewrite them):\n"
            f"  Before: {previous_heading or '(none)'}\n"
            f"  After: {next_heading or '(none)'}\n"
        )
    user = f"""Break this script scene into AI-video shot clips (coverage).

Scene {scene.get('order_index')}: {scene.get('heading') or '(no heading)'}
Scene budget: ~{scene_secs} seconds total across all clips
Typical clip length: {clip_cap} seconds (never longer — video models degrade beyond ~10s)
Suggested clip count: ~{n_clips_hint} (adjust to the material)
{context}
Characters in this scene:
{char_lines or '(none)'}

Full action (cover ALL of it across the clips):
{scene.get('action') or '(none)'}

Dialogue lines (verbatim, numbered):
{numbered_dialog}

=== HARD CONSTRAINTS (non-negotiable) ===
1. FULL COVERAGE: every numbered dialogue line is covered by EXACTLY ONE clip (via
   dialog_lines_covered), and together the clips' descriptions perform the ENTIRE action —
   no skipped beats, no duplicated dialogue.
2. Order: clip 1 plays first, then 2, ... matching the action's chronology.
3. Each clip's description is a self-contained video prompt: framing + subject + motion +
   setting, present tense, 1–4 sentences. Include a character anchor (hair/outfit feature)
   the first time a character appears in a clip.
4. duration_sec per clip: 3–{clip_cap}; the SUM should be approximately the scene budget.
5. shot_size is one of: wide, medium, closeUp, insert, overShoulder.
6. Set chain_from_prev=true ONLY when this clip should literally continue the previous
   clip's rendered frames (same camera take / motion carry). Usually false — a cut is fine.

Respond ONLY with JSON:
{{
  "clips": [
    {{
      "order_index": 1,
      "description": "Wide establishing shot…",
      "dialog_lines_covered": [],
      "shot_size": "wide",
      "duration_sec": 6,
      "chain_from_prev": false
    }}
  ]
}}

FINAL CHECK: every dialogue line number appears exactly once across all clips, durations
sum ≈ {scene_secs}, no clip exceeds {clip_cap}s. If not, fix it."""
    return [
        {
            "role": "system",
            "content": (
                "You are a film director planning shot coverage for AI video generation. "
                "You output ONLY a single valid JSON object."
            ),
        },
        {"role": "user", "content": user},
    ]


def _normalize_clips(
    raw_clips: list[dict[str, Any]],
    *,
    n_dialog_lines: int,
    scene_budget: int,
    clip_cap: int,
) -> list[dict[str, Any]]:
    """Validate + clamp the LLM's clip list; derive missing fields deterministically."""
    valid_sizes = {"wide", "medium", "closeUp", "insert", "overShoulder"}
    seen_lines: set[int] = set()
    clips: list[dict[str, Any]] = []
    for i, c in enumerate(raw_clips, start=1):
        if not isinstance(c, dict):
            continue
        covered = c.get("dialog_lines_covered") or []
        if not isinstance(covered, list):
            covered = []
        covered = sorted(
            {
                int(n)
                for n in covered
                if isinstance(n, int) and 1 <= n <= n_dialog_lines and n not in seen_lines
            }
        )
        seen_lines.update(covered)
        shot_size = c.get("shot_size")
        if shot_size not in valid_sizes:
            shot_size = None
        duration = c.get("duration_sec")
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            duration = None
        if not duration or duration < 2:
            duration = None
        clips.append(
            {
                "order_index": c.get("order_index") if isinstance(c.get("order_index"), int) else i,
                "description": (c.get("description") or "").strip() or None,
                "dialog_lines_covered": covered,
                "shot_size": shot_size,
                "duration_sec": min(duration or clip_cap, clip_cap),
                "chain_from_prev": bool(c.get("chain_from_prev")),
            }
        )
    # Renumber 1..N in the order given (models restart indexes per chunk).
    for i, c in enumerate(clips, start=1):
        c["order_index"] = i
    if not clips:
        return clips
    # Uncovered dialogue is a coverage hole — attach any missed lines to the
    # first clip whose description plausibly plays speech, else the last clip.
    missing = [n for n in range(1, n_dialog_lines + 1) if n not in seen_lines]
    if missing and clips:
        clips[-1]["dialog_lines_covered"] = sorted(
            set(clips[-1]["dialog_lines_covered"]) | set(missing)
        )
    # Re-normalize durations to the scene budget when the model overshot/undershot
    # wildly (>±35%); keep per-clip values inside the cap.
    total = sum(c["duration_sec"] for c in clips)
    if scene_budget > 0 and not (0.65 <= total / scene_budget <= 1.35):
        scale = scene_budget / total
        for c in clips:
            c["duration_sec"] = max(2, min(clip_cap, round(c["duration_sec"] * scale)))
    return clips


def coverage_briefs(
    project_id: int,
    scene_ids: list[int],
    *,
    guidance: str | None = None,
    clip_cap: int | None = None,
) -> list[dict[str, Any]]:
    """What the coverage model would receive per scene — for an MCP client."""
    cap = clip_cap or DEFAULT_CLIP_DURATION_SEC
    conn = get_db(settings.db_path)
    try:
        all_scenes = [
            row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM scenes WHERE project_id = ? ORDER BY order_index", (project_id,)
            ).fetchall()
        ]
        characters = [
            row_to_dict(r)
            for r in conn.execute(
                "SELECT id, name, appearance FROM characters WHERE project_id = ?", (project_id,)
            ).fetchall()
        ]
    finally:
        conn.close()
    wanted = set(scene_ids)
    briefs: list[dict[str, Any]] = []
    for scene in all_scenes:
        if scene["id"] not in wanted:
            continue
        messages = _coverage_messages(
            scene=scene,
            characters=characters,
            previous_heading=next(
                (s["heading"] for s in all_scenes if s["order_index"] < scene["order_index"]), None
            ),
            next_heading=next(
                (s["heading"] for s in all_scenes if s["order_index"] > scene["order_index"]), None
            ),
            clip_cap=cap,
        )
        if guidance:
            messages[1]["content"] += f"\n\nExtra direction from the user: {guidance}"
        briefs.append(
            {
                "scene_id": scene["id"],
                "order": scene["order_index"],
                "system": messages[0]["content"],
                "user": messages[1]["content"],
            }
        )
    return briefs


async def expand_scene_coverage(
    project_id: int,
    scene_ids: list[int] | None = None,
    *,
    guidance: str | None = None,
    clip_cap: int | None = None,
    provided: dict[int, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Break scenes into shot clips, replacing each scene's existing clips.

    Expands ALL of the project's scenes when scene_ids is None. Each expanded
    scene's old clips are deleted inside the same transaction that inserts the
    new ones (its chain_from_prev migrates onto clip #1). Returns a summary.

    ``provided`` maps scene id → clip list written by an MCP client: those
    scenes skip the LLM and go through the same validation (_normalize_clips)
    and replace transaction.
    """
    cap = clip_cap or DEFAULT_CLIP_DURATION_SEC
    conn = get_db(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM scenes WHERE project_id = ? ORDER BY order_index",
            (project_id,),
        ).fetchall()
        all_scenes = [row_to_dict(r) for r in rows]
        if scene_ids:
            wanted = set(scene_ids)
            scenes = [s for s in all_scenes if s["id"] in wanted]
        else:
            scenes = all_scenes
        if not scenes:
            raise ValueError("No scenes to expand — generate a script first")

        char_rows = conn.execute(
            "SELECT id, name, appearance FROM characters WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        characters = [row_to_dict(r) for r in char_rows]

        results: list[dict[str, Any]] = []
        total = len(scenes)
        for idx, scene in enumerate(scenes, start=1):
            prev_heading = next(
                (s["heading"] for s in all_scenes if s["order_index"] < scene["order_index"]),
                None,
            )
            next_heading = next(
                (s["heading"] for s in all_scenes if s["order_index"] > scene["order_index"]),
                None,
            )
            await event_bus.publish(
                "agent.thinking",
                {
                    "message": f"Breaking scene {scene['order_index']} into shots "
                    f"({idx}/{total})…",
                    "project_id": project_id,
                },
            )
            if provided is not None:
                raw_clips = [c for c in provided.get(scene["id"]) or [] if isinstance(c, dict)]
                if not raw_clips:
                    raise ValueError(f"Scene {scene['order_index']}: no clips supplied")
            else:
                messages = _coverage_messages(
                    scene=scene,
                    characters=characters,
                    previous_heading=prev_heading,
                    next_heading=next_heading,
                    clip_cap=cap,
                )
                if guidance:
                    messages[1]["content"] += f"\n\nExtra direction from the user: {guidance}"
                result = await generate_structured(messages, temperature=0.5)
                raw_clips = result.get("clips") or []
            if not raw_clips and provided is None:
                retry = [
                    messages[0],
                    {
                        "role": "user",
                        "content": messages[1]["content"]
                        + "\n\nPREVIOUS ATTEMPT FAILED: no clips array. You MUST return a "
                        "\"clips\" JSON array with at least one clip.",
                    },
                ]
                result = await generate_structured(retry, temperature=0.3)
                raw_clips = result.get("clips") or []
            if not raw_clips:
                raise ValueError(
                    f"Scene {scene['order_index']}: coverage pass returned no clips"
                )
            dialog_lines = _dialog_lines(scene.get("dialog"))
            budget = scene.get("duration_sec") or estimate_scene_duration_sec(scene)
            clips = _normalize_clips(
                raw_clips, n_dialog_lines=len(dialog_lines), scene_budget=budget, clip_cap=cap
            )

            # Replace-the-scene's-clips transaction. The scene-level chain flag
            # migrates onto clip #1; the default clip (backfill) had copied it.
            scene_chain = bool(scene.get("chain_from_prev"))
            conn.execute("DELETE FROM clips WHERE scene_id = ?", (scene["id"],))
            for c in clips:
                conn.execute(
                    """
                    INSERT INTO clips (scene_id, project_id, order_index, description,
                                       shot_size, dialog_lines_covered, duration_sec,
                                       workflow_id, chain_from_prev)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scene["id"],
                        project_id,
                        c["order_index"],
                        c["description"],
                        c["shot_size"],
                        json.dumps(c["dialog_lines_covered"]) if c["dialog_lines_covered"] else None,
                        c["duration_sec"],
                        scene.get("workflow_id"),
                        1 if (c["order_index"] == 1 and scene_chain) or c["chain_from_prev"] else 0,
                    ),
                )
            # The scene is now represented by its clips; clear its legacy
            # 1:1 render mirror so stale single-clip paths don't resurface.
            conn.execute(
                "UPDATE scenes SET video_path = NULL, chain_from_prev = 0 WHERE id = ?",
                (scene["id"],),
            )
            conn.commit()
            results.append(
                {
                    "scene_id": scene["id"],
                    "order_index": scene["order_index"],
                    "clips": len(clips),
                    # UI-facing aliases (api.ts contract) — same values.
                    "clip_count": len(clips),
                    "duration_total": sum(c["duration_sec"] for c in clips),
                    "total_duration_sec": sum(c["duration_sec"] for c in clips),
                }
            )
        return {
            "ok": True,
            "scenes": results,
            "total_clips": sum(r["clips"] for r in results),
        }
    finally:
        conn.close()
