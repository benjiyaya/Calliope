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


def _coverage_errors(
    raw_clips: Any,
    *,
    n_dialog_lines: int,
    clip_cap: int,
) -> list[str]:
    """Hard violations of the coverage contract, fed back for a rewrite.

    The old path only rejected an empty array; everything else (clips with no
    description, dialogue covered twice or not at all, clips over the cap) was
    silently patched by `_normalize_clips`, hiding a bad pass from the model
    that produced it."""
    errors: list[str] = []
    if not isinstance(raw_clips, list) or not raw_clips:
        return ['the "clips" JSON array is missing or empty — return at least one clip']
    seen: set[int] = set()
    for i, c in enumerate(raw_clips, start=1):
        if not isinstance(c, dict):
            errors.append(f"clip {i} is not a JSON object")
            continue
        if not str(c.get("description") or "").strip():
            errors.append(
                f'clip {i} has an empty "description" (framing + subject + motion + setting, '
                "1–4 sentences, with an explicit camera move)"
            )
        covered = c.get("dialog_lines_covered")
        if covered is not None and not isinstance(covered, list):
            errors.append(f'clip {i} "dialog_lines_covered" must be an array of line numbers')
            covered = []
        for n in covered or []:
            if not isinstance(n, int) or not (1 <= n <= n_dialog_lines):
                errors.append(f"clip {i} covers dialog line {n!r}, which is not 1..{n_dialog_lines}")
            elif n in seen:
                errors.append(f"dialog line {n} is covered by more than one clip")
            else:
                seen.add(n)
        try:
            duration = int(c.get("duration_sec"))
        except (TypeError, ValueError):
            duration = 0
        if not (1 <= duration <= clip_cap):
            errors.append(
                f"clip {i} duration_sec is {c.get('duration_sec')!r}; it must be 1–{clip_cap} seconds"
            )
    missing = [n for n in range(1, n_dialog_lines + 1) if n not in seen]
    if missing:
        errors.append(
            f"dialog lines {missing} are not covered by any clip — every line must be "
            "covered by EXACTLY ONE clip"
        )
    return errors


def _coverage_messages(
    *,
    scene: dict[str, Any],
    characters: list[dict[str, Any]],
    previous_scene: dict[str, Any] | None,
    next_scene: dict[str, Any] | None,
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
    if previous_scene or next_scene:
        prev_line = "(none)"
        if previous_scene:
            prev_state = (previous_scene.get("action") or "").strip()
            prev_line = (
                f"{previous_scene.get('heading') or '(no heading)'} — "
                f"ends with: {prev_state[-220:]}"
            )
        next_line = (next_scene or {}).get("heading") or "(none)" if next_scene else "(none)"
        context = (
            f"\nNeighbor scenes (for continuity, do NOT rewrite them):\n"
            f"  Before: {prev_line}\n"
            f"  After: {next_line}\n"
            f"  Clips 1..N must continue FROM that state — same positions, props in hand,\n"
            f"  lighting and screen direction as the previous scene leaves them.\n"
        )
    anchors = "\n".join(
        f"- {c['name']}: {(c.get('appearance') or '').strip()} "
        f"(consistency prompt: {(c.get('consistency_prompt') or '').strip() or 'n/a'})"
        for c in characters
        if (c.get("appearance") or c.get("consistency_prompt"))
    )
    user = f"""Break this script scene into AI-video shot clips (coverage).

Scene {scene.get('order_index')}: {scene.get('heading') or '(no heading)'}
Scene budget: ~{scene_secs} seconds total across all clips
Typical clip length: {clip_cap} seconds (never longer — video models degrade beyond ~10s)
Suggested clip count: ~{n_clips_hint} (adjust to the material)
{context}
Characters in this scene:
{char_lines or '(none)'}

Character consistency anchors (BINDING — a clip description must never contradict these):
{anchors or '(none)'}

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
   setting, present tense, 1–4 sentences. The first time a character appears in a clip,
   reuse their BINDING anchor wording from "Character consistency anchors" above (hair,
   outfit, distinguishing feature) — never invent or change appearance across clips.
3b. Every clip's description names an explicit camera move (push-in, pan, tilt, tracking,
   orbit, handheld) — no "camera holds" during action beats; without camera direction the
   video comes out static.
4. duration_sec per clip: 3–{clip_cap}; the SUM should be approximately the scene budget.
   Fit the dialogue to the duration, never the other way round: speech runs ~4.5 Chinese
   characters or ~2.5 English words per second, so a clip's lines need
   (their length ÷ speech rate) + 1–3s of breathing room; a clip whose lines do not fit
   must shed them to the next clip (or split a long speech across consecutive clips).
5. shot_size is one of: wide, medium, closeUp, insert, overShoulder. Vary framing across
   the scene's clips (progress wide → medium → close-up as tension rises); do not give
   every clip the same shot_size.

Respond ONLY with JSON:
{{
  "clips": [
    {{
      "order_index": 1,
      "description": "Wide establishing shot…",
      "dialog_lines_covered": [],
      "shot_size": "wide",
      "duration_sec": 6
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


async def expand_scene_coverage(
    project_id: int,
    scene_ids: list[int] | None = None,
    *,
    guidance: str | None = None,
    clip_cap: int | None = None,
) -> dict[str, Any]:
    """Break scenes into shot clips, replacing each scene's existing clips.

    Expands ALL of the project's scenes when scene_ids is None. Each expanded
    scene's old clips are deleted inside the same transaction that inserts the
    new ones. Returns a summary.
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
            "SELECT id, name, appearance, consistency_prompt FROM characters "
            "WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        characters = [row_to_dict(r) for r in char_rows]

        results: list[dict[str, Any]] = []
        total = len(scenes)
        for idx, scene in enumerate(scenes, start=1):
            prev_row = next(
                (s for s in all_scenes if s["order_index"] < scene["order_index"]),
                None,
            )
            next_row = next(
                (s for s in all_scenes if s["order_index"] > scene["order_index"]),
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
            messages = _coverage_messages(
                scene=scene,
                characters=characters,
                previous_scene=prev_row,
                next_scene=next_row,
                clip_cap=cap,
            )
            if guidance:
                messages[1]["content"] += f"\n\nExtra direction from the user: {guidance}"
            dialog_lines = _dialog_lines(scene.get("dialog"))
            result = await generate_structured(messages, temperature=0.5)
            raw_clips = result.get("clips")
            errors = _coverage_errors(
                raw_clips, n_dialog_lines=len(dialog_lines), clip_cap=cap
            )
            if errors:
                # Reject-and-rewrite: hand the exact violations back for a
                # low-temperature rewrite instead of silently patching them.
                retry = [
                    messages[0],
                    {
                        "role": "user",
                        "content": messages[1]["content"]
                        + "\n\nPREVIOUS ATTEMPT REJECTED — fix EVERY problem below:\n"
                        + "\n".join(f"- {e}" for e in errors),
                    },
                ]
                result = await generate_structured(retry, temperature=0.3)
                raw_clips = result.get("clips")
                errors = _coverage_errors(
                    raw_clips, n_dialog_lines=len(dialog_lines), clip_cap=cap
                )
                if errors:
                    logger.warning(
                        "scene %s: coverage rewrite still invalid (%s); normalizing",
                        scene["order_index"],
                        "; ".join(errors),
                    )
            if not raw_clips:
                raise ValueError(
                    f"Scene {scene['order_index']}: coverage pass returned no clips"
                )
            budget = scene.get("duration_sec") or estimate_scene_duration_sec(scene)
            clips = _normalize_clips(
                raw_clips, n_dialog_lines=len(dialog_lines), scene_budget=budget, clip_cap=cap
            )

            # Replace-the-scene's-clips transaction.
            conn.execute("DELETE FROM clips WHERE scene_id = ?", (scene["id"],))
            for c in clips:
                conn.execute(
                    """
                    INSERT INTO clips (scene_id, project_id, order_index, description,
                                       shot_size, dialog_lines_covered, duration_sec,
                                       workflow_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                    ),
                )
            # The scene is now represented by its clips; clear its legacy
            # 1:1 render mirror so stale single-clip paths don't resurface.
            conn.execute(
                "UPDATE scenes SET video_path = NULL WHERE id = ?",
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
