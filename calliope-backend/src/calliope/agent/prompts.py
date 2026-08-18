"""Prompts for Calliope story / script / asset generation."""
from __future__ import annotations

import re
from typing import Any


STORY_GENERATION_SYSTEM = (
    "You are a story development assistant for an AI video studio. "
    "You output ONLY a single valid JSON object. "
    "HARD RULE: the beats array length must equal required_beat_count from the user message. "
    "Returning fewer beats is a failure. Longer target runtimes require many fine-grained beats."
)

SCRIPT_GENERATION_SYSTEM = (
    "You are a screenwriter for short-form AI video. "
    "Write a scene-by-scene script that uses the provided characters and locations. "
    "Each scene's action is fed directly into a video generation model, so it must be a "
    "rich, multi-sentence cinematic description — never a single short sentence. "
    "Always respond with a single valid JSON object. "
    "HARD RULE: the scenes array length must equal required_scene_count from the user message. "
    "Returning fewer scenes is a failure. Respect the user's chosen scene count."
)


def estimate_target_seconds(target_duration: str | None) -> int:
    """Best-effort parse of free-text duration into seconds."""
    text = (target_duration or "").strip().lower()
    if not text:
        return 30

    # Explicit "N scenes" -> ~8s each as a coarse floor for beat planning
    scenes_m = re.search(r"(\d+)\s*scenes?", text)
    if scenes_m and "min" not in text and "sec" not in text:
        return max(30, int(scenes_m.group(1)) * 8)

    minutes = 0.0
    seconds = 0.0
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(min|mins|minutes?|m)\b", text):
        minutes += float(m.group(1))
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(sec|secs|seconds?|s)\b", text):
        seconds += float(m.group(1))

    if minutes or seconds:
        return max(15, int(minutes * 60 + seconds))

    bare = re.search(r"\b(\d+(?:\.\d+)?)\b", text)
    if bare:
        n = float(bare.group(1))
        if "medium" in text or n >= 2:
            if n <= 30:
                return int(n * 60)
        if n <= 180:
            return int(n)

    if "medium" in text:
        return 120
    if "short" in text or "brief" in text:
        return 30
    if "long" in text or "feature" in text:
        return 180
    return 60


def recommend_beat_count(target_duration: str | None) -> int:
    """
    Story beats for the AI video pipeline (~12s of narrative weight each).
    30s -> 4, 2 min -> 10, 5 min -> 25, 10 min -> 50 (capped at 60).
    """
    secs = estimate_target_seconds(target_duration)
    return max(4, min(60, round(secs / 12)))


def recommend_scene_count(target_duration: str | None) -> int:
    """Video scenes (~7s each) for the target runtime."""
    secs = estimate_target_seconds(target_duration)
    return max(4, min(90, round(secs / 7)))


def story_generation_user_prompt(
    title: str | None,
    idea: str | None,
    genre: str | None,
    tone: str | None,
    target_duration: str | None,
) -> str:
    secs = estimate_target_seconds(target_duration)
    beat_n = recommend_beat_count(target_duration)
    return f"""Create a story brief for an AI-generated video.

required_beat_count: {beat_n}
estimated_runtime_seconds: {secs}
target_duration_text: {target_duration or "short (~30 seconds)"}

Title: {title or "Untitled"}
Idea: {idea or "No idea provided."}
Genre: {genre or "Not specified"}
Tone: {tone or "cinematic, atmospheric"}

=== HARD CONSTRAINTS (non-negotiable) ===
1. The JSON field "beats" MUST contain EXACTLY {beat_n} objects.
2. order_index must run 1, 2, 3, ... {beat_n} with no gaps.
3. Do NOT summarize into a short arc. Do NOT return 5-8 beats for a long runtime.
4. For ~{secs}s (~{secs // 60} min), beats are fine-grained plot steps (~12 seconds of story weight each) so Script can later expand into many short video clips.
5. Cover full arc across all {beat_n} beats: setup, rising complications, midpoint turn, escalation, climax, resolution — spread across the whole list, not compressed into the first few items.
6. If you are unsure, add more concrete incident beats rather than fewer abstract ones.

=== OUTPUT SCHEMA (JSON only) ===
{{
  "title": "a compelling story title",
  "logline": "one sentence hook",
  "beats": [
    {{"order_index": 1, "title": "Beat title", "description": "what happens visually / dramatically"}}
  ],
  "characters": [
    {{
      "name": "Name",
      "role": "protagonist|antagonist|supporting",
      "age": "approximate age",
      "appearance": "concise visual description for image generation",
      "personality": "concise personality traits"
    }}
  ],
  "locations": [
    {{
      "name": "Location name",
      "description": "concise visual description for image generation"
    }}
  ]
}}

Keep descriptions visual and concrete. Characters and locations should be reusable across scenes.
FINAL CHECK before responding: beats.length == {beat_n}. If not, fix it."""


def build_story_messages(
    title: str | None,
    idea: str | None,
    genre: str | None,
    tone: str | None,
    target_duration: str | None,
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": STORY_GENERATION_SYSTEM},
        {
            "role": "user",
            "content": story_generation_user_prompt(title, idea, genre, tone, target_duration),
        },
    ]


def build_script_messages(
    *,
    title: str,
    idea: str | None,
    beats: list[dict[str, Any]],
    characters: list[dict[str, Any]],
    locations: list[dict[str, Any]],
    target_duration: str | None,
    scene_count: int | None = None,
) -> list[dict[str, str]]:
    secs = estimate_target_seconds(target_duration)
    recommended = recommend_scene_count(target_duration)
    # Honor an explicit / existing count (e.g. user added empty scenes before regenerate)
    scene_n = max(recommended, int(scene_count)) if scene_count and scene_count > 0 else recommended
    # Stretch runtime floor when the user asked for more clips than duration alone implies
    secs = max(secs, scene_n * 6)
    duration_hint = max(4, round(secs / scene_n))
    char_lines = "\n".join(
        f"- id={c['id']} {c['name']} ({c.get('role') or ''}): {c.get('appearance') or ''}"
        for c in characters
    )
    loc_lines = "\n".join(
        f"- id={l['id']} {l['name']}: {l.get('description') or ''}" for l in locations
    )
    beat_lines = "\n".join(
        f"- {b.get('order_index')}. {b.get('title')}: {b.get('description')}" for b in beats
    )
    user = f"""Write a scene script for this project.

Title: {title}
Idea: {idea or ''}
Target duration: {target_duration or 'short (~30-60 seconds)'}
Estimated runtime: ~{secs} seconds
required_scene_count: {scene_n}

Story beats:
{beat_lines or '(none)'}

Characters:
{char_lines or '(none)'}

Locations:
{loc_lines or '(none)'}

=== HARD CONSTRAINTS (non-negotiable) ===
1. The JSON field "scenes" MUST contain EXACTLY {scene_n} objects.
2. order_index must run 1, 2, 3, ... {scene_n} with no gaps.
3. Do NOT collapse back to fewer scenes. The user expanded the script to {scene_n} clips — fill all of them.
4. Sum of duration_sec across all scenes should be approximately {secs} (within ±15%).
5. Prefer short scenes (5–10s) — this is for AI video clips. Spread the beat arc across all {scene_n} scenes.

=== ACTION DETAIL (this text IS the video prompt — users copy it straight into the generator) ===
Each scene's "action" is fed verbatim to an AI video generator. Write 4–6 vivid present-tense
sentences (~80–130 words) per scene as ONE flowing natural-English paragraph — no bullet points,
no labeled fragments (do NOT write "Shot:", "Lighting:", etc.), no line breaks. Rules:
1. ONE SHOT PER SCENE. A clip is only {duration_hint} seconds — describe a single continuous
   camera setup with at most one simple action beat (one entrance, one gesture, one reveal).
   If a beat needs more room, split it across multiple scenes instead of compressing it.
2. SHOT & MOTION. Name the framing and camera move (wide establishing, slow push-in, handheld
   tracking, low angle, rack focus) AND what visibly moves in frame — video models need motion:
   gestures, turning heads, hair and fabric, drifting dust, sweeping flashlight beams, an
   expression shifting. A static description produces a static clip.
3. CHARACTER ANCHORS. The first time each character appears in a scene, attach a 3–8 word
   visual anchor taken from their description above (hair, outfit, one distinguishing
   feature), e.g. "MIA, a teenage girl with a chestnut ponytail and yellow rain jacket,".
   Later mentions in the same scene use the plain name. Never invent new appearance details —
   reuse these anchors so every clip matches the same face and wardrobe.
4. ENVIRONMENT & CONTINUITY. Concrete set details, props, weather, time-of-day — consistent
   with the location description and with earlier scenes set in the same location.
5. LIGHTING & MOOD. Named light sources, color palette, emotional tone of the moment.
Describe only what is VISIBLE (no inner thoughts; no sounds or music — dialogue covers audio).
Vary shot types across scenes — do not write {scene_n} identical wide shots.

=== DIALOGUE ===
Keep lines short and natural. When delivery matters for performance, add a brief cue in
parentheses after the speaker name, e.g. "MIA (whispering): line" or "LIAM (hushed, excited): line".

Respond ONLY with JSON:
{{
  "scenes": [
    {{
      "order_index": 1,
      "heading": "INT. LOCATION - TIME",
      "action": "Wide establishing shot, slow push-in through the cracked main doorway. MIA, a teenage girl with a chestnut ponytail and yellow rain jacket, steps into the dusty main hall, lantern held high, its warm glow catching drifting dust motes around her cautious, widening eyes. She freezes mid-step, fingers tightening on the lantern handle as she looks up. Overturned desks and a collapsed chalkboard fill the frame; moonlight cuts through shattered windows in pale blue shafts. The mood is hushed and uneasy, shadows pooling at the edges of the lantern light.",
      "dialog": "MIA (whispering): line\\nNARRATOR: line",
      "duration_sec": 5,
      "character_ids": [1],
      "location_id": 1
    }}
  ]
}}

Use only the provided character_ids and location_ids.
FINAL CHECK before responding: scenes.length == {scene_n}, and every action is a flowing prose
paragraph of 4–6 sentences (~80–130 words) covering ONE shot, with a character anchor at each
character's first mention (no bullets, no labels, no invented appearances). If not, fix it."""
    return [
        {"role": "system", "content": SCRIPT_GENERATION_SYSTEM},
        {"role": "user", "content": user},
    ]


def character_sheet_prompt(character: dict[str, Any]) -> str:
    """Transparent character-sheet template — what ComfyUI receives for turnarounds."""
    name = (character.get("name") or "Unnamed").strip()
    role = (character.get("role") or "character").strip()
    age = (character.get("age") or "unspecified age").strip()
    appearance = (character.get("appearance") or "no appearance notes yet").strip()
    personality = (character.get("personality") or "").strip()
    personality_line = f"Personality cues (visual only): {personality}\n" if personality else ""
    return (
        f"CHARACTER SHEET — {name}\n"
        f"Role: {role}. Age: {age}.\n"
        f"Appearance: {appearance}\n"
        f"{personality_line}"
        f"\n"
        f"Layout: single cinematic character reference sheet on a clean neutral backdrop, "
        f"multiple panels — front full body, side full body, back full body, three-quarter, "
        f"and a head close-up. Same face, hair, body proportions, and outfit in every panel. "
        f"Neutral standing pose, even studio lighting, high detail, no text watermarks, "
        f"no extra characters."
    )


def character_portrait_prompt(character: dict[str, Any]) -> str:
    """Transparent portrait template — face/upper-body lock for consistency."""
    name = (character.get("name") or "Unnamed").strip()
    role = (character.get("role") or "character").strip()
    age = (character.get("age") or "unspecified age").strip()
    appearance = (character.get("appearance") or "no appearance notes yet").strip()
    return (
        f"CHARACTER PORTRAIT — {name}\n"
        f"Role: {role}. Age: {age}.\n"
        f"Appearance: {appearance}\n"
        f"\n"
        f"Shot: cinematic head-and-shoulders portrait, eye-level, soft key light, "
        f"shallow depth of field, sharp facial features, consistent wardrobe collar/shoulders, "
        f"plain muted background, photoreal or high-end concept art, no text."
    )


def location_reference_prompt(location: dict[str, Any]) -> str:
    """Transparent environment reference template."""
    name = (location.get("name") or "Unnamed place").strip()
    description = (location.get("description") or "no description yet").strip()
    return (
        f"ENVIRONMENT REFERENCE — {name}\n"
        f"Description: {description}\n"
        f"\n"
        f"Shot: wide establishing concept art, cinematic atmosphere, clear readable space "
        f"for characters to stand in, consistent lighting and materials, no people, no text."
    )


def character_image_prompt(character: dict[str, Any], *, kind: str = "sheet") -> str:
    """
    Prefer the user-edited consistency_prompt when present.
    Otherwise fill the published template (never invent a hidden LLM prompt).
    """
    saved = (character.get("consistency_prompt") or "").strip()
    if saved:
        return saved
    if kind == "portrait":
        return character_portrait_prompt(character)
    return character_sheet_prompt(character)


def location_image_prompt(location: dict[str, Any]) -> str:
    saved = (location.get("consistency_prompt") or "").strip()
    if saved:
        return saved
    return location_reference_prompt(location)


def scene_video_prompt(scene: dict[str, Any], characters: list[dict[str, Any]]) -> str:
    char_bits = ", ".join(
        f"{c.get('name')}: {c.get('consistency_prompt') or c.get('appearance') or ''}"
        for c in characters
    )
    parts = [
        scene.get("heading") or "",
        scene.get("action") or "",
        f"featuring {char_bits}" if char_bits else "",
        "cinematic motion, coherent continuity",
    ]
    return ". ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# MiniMax H3 full-reference (ref2video) prompt profile
# ---------------------------------------------------------------------------
# Condensed from MiniMax's VIDEO_PROMPT_WRITING_GUIDE_ref_en.md, adapted to
# Calliope's single-scene clips: each subject's reference image is wired into
# the workflow's generic (Input:image) slots in node-id order, and that order
# defines the <Subject N> / <Picture N> numbering below.

MINIMAX_H3_REF_SYSTEM = (
    "You rewrite scene descriptions into MiniMax H3's full-reference video prompt format. "
    "Output ONLY the rewritten prompt as plain text — no markdown fences, no commentary. "
    "The output must contain exactly these six sections in this order, each starting with "
    "its header on its own line:\n"
    "subject_definitions:\n"
    "summary:\n"
    "retention_analysis:\n"
    "detailed_description:\n"
    "overall_soundscape:\n"
    "non_diegetic_music:\n"
    "\n"
    "Rules:\n"
    "1. subject_definitions: one line per referenced subject supplied in the user message, "
    "keeping its exact <Subject N> index. Form: '<Subject N> is the <description> in "
    "<Picture N>, with <key visual features to preserve>.' <Picture N> is the reference "
    "image wired to slot N — cite it inside the subject line, never as a standalone entry.\n"
    "2. summary: one short English paragraph starting with '[reference generation]' that "
    "states what happens using the <Subject N> labels.\n"
    "3. retention_analysis: one line per subject: '<Subject N> (appears in [Shot 1]…): "
    "fully_preserved - <which defined features are retained>.'\n"
    "4. detailed_description: the main body, 120–300 words. Open with one or two style "
    "sentences (lighting, palette, medium) BEFORE '[Shot 1]'. '[Shot 1]' has no timestamp; "
    "later cuts use '[Shot N] At MM:SS.mmm, …'. For clips under ~8 seconds prefer a single "
    "shot. Introduce each <Subject N> at its first visible appearance with its referenced "
    "features, position, and action; reuse the label afterwards. Describe only what is "
    "visible except sound/dialogue.\n"
    "5. Dialogue: give each speaker a stable ID in order of first speech — '<Subject N> (S1) "
    "says, <d>[English] …</d>'. A speaker with no defined subject uses a stable voice "
    "description, e.g. 'A narrator (S2) says, <d>[English] …</d>'. Keep the original "
    "language of every line inside <d> and tag it, e.g. [English], [Chinese].\n"
    "6. overall_soundscape: ambience and physical sounds across the clip, or 'N/A'. "
    "non_diegetic_music: audience-only score (instrumentation, tempo), or 'N/A'.\n"
    "Write everything in English except dialogue/lyrics inside <d> and visible on-screen text."
)


def _subject_roster_lines(subjects: list[dict[str, Any]]) -> str:
    lines = []
    for s in subjects:
        lines.append(
            f"<Subject {s['index']}> = {s['kind']} \"{s.get('name') or 'unnamed'}\" "
            f"(reference image slot {s['index']}): {s.get('appearance') or 'no description'}"
        )
    return "\n".join(lines)


def build_minimax_h3_ref_messages(
    scene: dict[str, Any], subjects: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """LLM messages that rewrite a scene into H3's six-section ref format.

    subjects: ordered roster — index N corresponds to ref image slot N.
    """
    roster = _subject_roster_lines(subjects) or (
        "(no reference images — describe subjects from the action text)"
    )
    user = f"""Rewrite this scene into MiniMax H3 full-reference format.

Scene heading: {scene.get('heading') or '(none)'}
Scene duration: ~{scene.get('duration_sec') or 6} seconds

Action (visual base for detailed_description):
{scene.get('action') or '(none)'}

Dialogue (raw 'SPEAKER: line' format; optional '(delivery cue)' after the speaker —
map speakers to subjects by name and keep cues as delivery direction):
{scene.get('dialog') or '(none)'}

Referenced subjects (keep these exact indices):
{roster}
"""
    return [
        {"role": "system", "content": MINIMAX_H3_REF_SYSTEM},
        {"role": "user", "content": user},
    ]


def minimax_h3_ref_fallback(scene: dict[str, Any], subjects: list[dict[str, Any]]) -> str:
    """Deterministic six-section H3 prompt — used when the LLM rewrite fails."""
    defs = []
    retention = []
    for s in subjects:
        name = s.get("name") or "unnamed"
        desc = (s.get("appearance") or "").strip()
        defs.append(
            f"<Subject {s['index']}> is the {s['kind']} \"{name}\" in "
            f"<Picture {s['index']}>" + (f", {desc}." if desc else ".")
        )
        retention.append(
            f"<Subject {s['index']}> (appears in [Shot 1]): fully_preserved - "
            f"the referenced appearance of \"{name}\" is retained."
        )

    heading = (scene.get("heading") or "").strip()
    action = (scene.get("action") or "").strip()
    labels = ", ".join(f"<Subject {s['index']}>" for s in subjects)
    summary = (
        f"[reference generation] {heading or 'A scene'} featuring {labels}."
        if labels
        else f"[reference generation] {heading or 'A scene'}."
    )

    body = (
        "The target video is in a cinematic style consistent with the reference images, "
        "with coherent lighting and natural motion.\n"
        f"[Shot 1] {heading} {action}".strip()
    )

    # Map 'SPEAKER: line' rows onto subjects by name; assign speaker IDs in speech order.
    # An optional delivery cue — 'MIA (whispering): line' — is kept as performance direction.
    dialog_lines = []
    speaker_ids: dict[str, int] = {}
    name_to_subject = {(s.get("name") or "").strip().lower(): s["index"] for s in subjects}
    for raw in (scene.get("dialog") or "").splitlines():
        if ":" not in raw:
            continue
        speaker, line = raw.split(":", 1)
        speaker, line = speaker.strip(), line.strip()
        if not line:
            continue
        cue_m = re.search(r"\(([^)]*)\)\s*$", speaker)
        cue = cue_m.group(1).strip() if cue_m else ""
        base = re.sub(r"\s*\([^)]*\)\s*$", "", speaker).strip() or speaker
        key = base.lower()
        if key not in speaker_ids:
            speaker_ids[key] = len(speaker_ids) + 1
        sid = speaker_ids[key]
        subj_idx = name_to_subject.get(key)
        who = f"<Subject {subj_idx}> (S{sid})" if subj_idx else f"{base.title()} (S{sid})"
        dialog_lines.append(f"{who} says{f' {cue}' if cue else ''}, <d>[English] {line}</d>")
    if dialog_lines:
        body += "\n" + "\n".join(dialog_lines)

    return (
        "subject_definitions:\n" + ("\n".join(defs) if defs else "N/A") + "\n\n"
        "summary:\n" + summary + "\n\n"
        "retention_analysis:\n" + ("\n".join(retention) if retention else "N/A") + "\n\n"
        "detailed_description:\n" + body + "\n\n"
        "overall_soundscape:\nN/A\n\n"
        "non_diegetic_music:\nN/A"
    )
