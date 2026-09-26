"""Project continuity ledger: one timed film plan shared by every H3 clip.

The MiniMax compiler phrases a clip. This module decides the facts it must
keep — style, bindings, camera, lighting, and what changed after the previous
shot — so neighboring clips are slices of one plan.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from calliope.agent.llm import LLMClient, extract_json
from calliope.config import settings
from calliope.db import get_db, row_to_dict

logger = logging.getLogger("calliope.continuity")

PLAN_TIMEOUT_SEC = 25.0
CRITIC_TIMEOUT_SEC = 15.0

OVERVIEW_KEYS = ("style", "pace", "soundscape", "camera_grammar", "lighting")
REQUIREMENT_KEYS = (
    "style",
    "subjects",
    "actions",
    "dialogue",
    "sound",
    "camera",
    "lighting",
    "spatial",
    "scene",
)
SHOT_KEYS = (
    "window",
    "action",
    "speakers",
    "camera",
    "lighting",
    "spatial",
    "state_after",
)
_MEDIA_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".mp4",
    ".webm",
    ".mov",
    ".mkv",
    ".avi",
    ".m4v",
}

_PLAN_SYSTEM = """You write one continuity ledger for a whole short film.
Return a single JSON object and nothing else:
{
  "overview": {"style": "", "pace": "", "soundscape": "", "camera_grammar": "", "lighting": ""},
  "requirements": {
    "style": "", "subjects": "", "actions": "", "dialogue": "", "sound": "",
    "camera": "", "lighting": "", "spatial": "", "scene": ""
  },
  "shots": [
    {
      "clip_id": 0,
      "window": "00:00.000-00:06.000",
      "action": "",
      "speakers": "",
      "camera": "",
      "lighting": "",
      "spatial": "",
      "state_after": ""
    }
  ]
}
Rules:
- One shot per clip_id in the board, same playback order. Do not invent clip ids.
- requirements: nine short locked lines. Accept later paraphrase; do not leave a bucket blank when the board states it.
- speakers bind each dialogue line to the person who says it. Do not move a line.
- camera and lighting name the move and the light state. state_after is what changed for the next shot.
- A clip marked REALIZED already has footage. Keep its action, speakers, camera, and lighting aligned with that footage; do not restage it.
- Reference image and video paths are identity and motion. Do not recast a subject because a location description mentions someone else.
"""

_CRITIC_SYSTEM = """You check one clip prompt against a film continuity plan.
Nine checks: style, subjects, actions, dialogue, sound, camera, lighting, spatial relations, scene.
Accept paraphrase and compatible elaboration.
Report only failures: omission, wrong subject binding, wrong speaker–dialogue binding, contradiction, or a shot-order conflict with the neighbor shots.
Return JSON only: {"ok": true, "notes": []} when it holds, or {"ok": false, "notes": ["short failure", ...]}.
No second draft. No praise.
"""


def media_paths(values: dict[str, Any] | None) -> list[str]:
    """Path-like strings in a clip form, in stable key order."""
    if not isinstance(values, dict):
        return []
    found: list[str] = []
    for key in sorted(values, key=lambda item: str(item)):
        raw = values[key]
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if not text:
            continue
        suffix = Path(text).suffix.lower()
        if suffix in _MEDIA_SUFFIXES or "/" in text or "\\" in text:
            found.append(text)
    return found


def _ts(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    whole = int(seconds)
    millis = int(round((seconds - whole) * 1000))
    if millis >= 1000:
        whole += 1
        millis -= 1000
    minutes, secs = divmod(whole, 60)
    return f"{minutes:02d}:{secs:02d}.{millis:03d}"


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clip_dialog_text(clip: dict[str, Any]) -> str:
    covered = clip.get("dialog_lines_covered")
    dialog = clip.get("dialog") or ""
    if not covered:
        return ""
    if isinstance(covered, str):
        try:
            covered = json.loads(covered)
        except (json.JSONDecodeError, TypeError):
            return ""
    if not isinstance(covered, list):
        return ""
    lines = [ln for ln in str(dialog).splitlines() if ln.strip()]
    picked = [
        lines[i - 1]
        for i in covered
        if isinstance(i, int) and 1 <= i <= len(lines)
    ]
    return "\n".join(picked)


def _input_values(clip: dict[str, Any]) -> dict[str, Any]:
    raw = clip.get("video_settings_json")
    if not raw:
        return {}
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    values = data.get("input_values")
    return values if isinstance(values, dict) else {}


def _clip_ref_paths(
    clip: dict[str, Any], live_refs: dict[int, list[str]] | None
) -> list[str]:
    stored = media_paths(_input_values(clip))
    cid = int(clip["id"])
    if not live_refs or cid not in live_refs:
        return stored
    live = [p.strip() for p in live_refs[cid] if isinstance(p, str) and p.strip()]
    if live == stored:
        return stored
    return live


def load_board(project_id: int) -> dict[str, Any]:
    """Screenplay, shots, cast, and places for one project."""
    conn = get_db(settings.db_path)
    try:
        project = conn.execute(
            "SELECT id, title, idea, genre, tone, continuity_json FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if not project:
            raise ValueError(f"Project {project_id} not found")
        clips = [
            row_to_dict(r)
            for r in conn.execute(
                """
                SELECT c.*, s.order_index AS scene_order, s.heading, s.action, s.dialog,
                       s.location_id, s.env_image_path
                FROM clips c JOIN scenes s ON s.id = c.scene_id
                WHERE c.project_id = ?
                ORDER BY s.order_index, c.order_index, c.id
                """,
                (project_id,),
            ).fetchall()
        ]
        characters = [
            row_to_dict(r)
            for r in conn.execute(
                """
                SELECT id, name, appearance, consistency_prompt, sheet_path, portrait_path
                FROM characters WHERE project_id = ? ORDER BY id
                """,
                (project_id,),
            ).fetchall()
        ]
        locations = [
            row_to_dict(r)
            for r in conn.execute(
                """
                SELECT id, name, description, consistency_prompt, reference_image_path
                FROM locations WHERE project_id = ? ORDER BY id
                """,
                (project_id,),
            ).fetchall()
        ]
        return {
            "project": row_to_dict(project),
            "clips": clips,
            "characters": characters,
            "locations": locations,
        }
    finally:
        conn.close()


def basis_hash(
    board: dict[str, Any], live_refs: dict[int, list[str]] | None = None
) -> str:
    """Fingerprint of script text, clip order, and reference paths."""
    clips = []
    for clip in board["clips"]:
        clips.append(
            {
                "id": int(clip["id"]),
                "scene_order": int(clip.get("scene_order") or 0),
                "order_index": int(clip.get("order_index") or 0),
                "heading": clip.get("heading") or "",
                "action": clip.get("action") or "",
                "dialog": clip.get("dialog") or "",
                "description": clip.get("description") or "",
                "shot_size": clip.get("shot_size") or "",
                "dialog_lines_covered": clip.get("dialog_lines_covered") or "",
                "refs": _clip_ref_paths(clip, live_refs),
                "env_image_path": clip.get("env_image_path") or "",
                "clip_path": clip.get("clip_path") or "",
            }
        )
    characters = [
        {
            "id": int(c["id"]),
            "name": c.get("name") or "",
            "appearance": c.get("appearance") or "",
            "consistency_prompt": c.get("consistency_prompt") or "",
            "sheet_path": c.get("sheet_path") or "",
            "portrait_path": c.get("portrait_path") or "",
        }
        for c in board["characters"]
    ]
    locations = [
        {
            "id": int(loc["id"]),
            "name": loc.get("name") or "",
            "description": loc.get("description") or "",
            "consistency_prompt": loc.get("consistency_prompt") or "",
            "reference_image_path": loc.get("reference_image_path") or "",
        }
        for loc in board["locations"]
    ]
    payload = json.dumps(
        {"clips": clips, "characters": characters, "locations": locations},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def deterministic_plan(board: dict[str, Any]) -> dict[str, Any]:
    """A locked plan built from the board when the plan model is unavailable."""
    project = board["project"]
    names = ", ".join(c.get("name") or "" for c in board["characters"] if c.get("name"))
    places = ", ".join(loc.get("name") or "" for loc in board["locations"] if loc.get("name"))
    style = (project.get("tone") or project.get("genre") or "cinematic").strip()
    cursor = 0.0
    shots = []
    for clip in board["clips"]:
        dur = float(clip.get("duration_sec") or 6)
        start, end = cursor, cursor + dur
        cursor = end
        action = (clip.get("description") or clip.get("action") or clip.get("heading") or "").strip()
        shots.append(
            {
                "clip_id": int(clip["id"]),
                "window": f"{_ts(start)}-{_ts(end)}",
                "action": action[:500],
                "speakers": _clip_dialog_text(clip)[:500],
                "camera": (clip.get("shot_size") or "").strip(),
                "lighting": "",
                "spatial": "",
                "state_after": "",
            }
        )
    return {
        "overview": {
            "style": style[:400],
            "pace": "Hold the pace of the shot list.",
            "soundscape": "",
            "camera_grammar": "Keep screen direction consistent with the previous shot.",
            "lighting": "Keep the lighting state until a shot's action changes it.",
        },
        "requirements": {
            "style": style[:400],
            "subjects": (names or "Use only the reference images supplied for each clip.")[:400],
            "actions": "Perform each clip's action in playback order.",
            "dialogue": "Keep each line with the speaker written on that clip.",
            "sound": "",
            "camera": "Keep each clip's camera move and the film's screen direction.",
            "lighting": "Do not change lighting between neighbors unless the shot says so.",
            "spatial": (places or "Keep subjects on the sides established by the previous shot.")[:400],
            "scene": (project.get("idea") or project.get("title") or "")[:400],
        },
        "shots": shots,
        "based_on": "",
    }


def normalize_plan(raw: dict[str, Any], board: dict[str, Any]) -> dict[str, Any]:
    """Coerce a model plan onto the board's clip ids. Missing shots stay stubs."""
    base = deterministic_plan(board)
    overview = raw.get("overview") if isinstance(raw.get("overview"), dict) else {}
    for key in OVERVIEW_KEYS:
        val = str(overview.get(key) or "").strip()
        if val:
            base["overview"][key] = val[:400]
    req = raw.get("requirements") if isinstance(raw.get("requirements"), dict) else {}
    for key in REQUIREMENT_KEYS:
        if key in req:
            base["requirements"][key] = str(req.get(key) or "").strip()[:400]
    by_id: dict[int, dict[str, Any]] = {}
    for shot in raw.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        cid = _as_int(shot.get("clip_id"))
        if cid is None:
            continue
        by_id[cid] = shot
    merged = []
    for stub in base["shots"]:
        src = by_id.get(stub["clip_id"])
        row = dict(stub)
        if src:
            for key in SHOT_KEYS:
                if key in src:
                    row[key] = str(src.get(key) or "").strip()[:500]
        row["clip_id"] = stub["clip_id"]
        merged.append(row)
    base["shots"] = merged
    return base


def _parse_stored(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("shots"), list):
        return None
    return data


def _persist(project_id: int, plan: dict[str, Any]) -> None:
    stored = {
        "overview": plan.get("overview") or {},
        "requirements": plan.get("requirements") or {},
        "shots": plan.get("shots") or [],
        "based_on": plan.get("based_on") or "",
    }
    conn = get_db(settings.db_path)
    try:
        conn.execute(
            "UPDATE projects SET continuity_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (json.dumps(stored, ensure_ascii=False), project_id),
        )
        conn.commit()
    finally:
        conn.close()


def _board_digest(board: dict[str, Any]) -> str:
    project = board["project"]
    lines = [
        f"Title: {project.get('title') or ''}",
        f"Idea: {project.get('idea') or ''}",
        f"Genre: {project.get('genre') or ''}",
        f"Tone: {project.get('tone') or ''}",
        "Characters:",
    ]
    for c in board["characters"]:
        lines.append(
            f"- {c.get('name')}: appearance={c.get('appearance') or ''}; "
            f"consistency={c.get('consistency_prompt') or ''}; "
            f"sheet={c.get('sheet_path') or ''}; portrait={c.get('portrait_path') or ''}"
        )
    lines.append("Locations:")
    for loc in board["locations"]:
        lines.append(
            f"- {loc.get('name')}: {loc.get('description') or ''}; "
            f"consistency={loc.get('consistency_prompt') or ''}; "
            f"image={loc.get('reference_image_path') or ''}"
        )
    lines.append("Shots in playback order:")
    for clip in board["clips"]:
        realized = ""
        if clip.get("clip_path"):
            realized = f" REALIZED footage={clip['clip_path']}"
        lines.append(
            f"- clip_id={clip['id']} #{clip.get('scene_order')}.{clip.get('order_index')} "
            f"{clip.get('heading') or ''}{realized}"
        )
        lines.append(f"  description: {clip.get('description') or ''}")
        lines.append(f"  scene action: {clip.get('action') or ''}")
        lines.append(f"  dialogue: {clip.get('dialog') or ''}")
        lines.append(f"  lines covered: {clip.get('dialog_lines_covered') or ''}")
        lines.append(f"  shot size: {clip.get('shot_size') or ''}")
        lines.append(f"  refs: {', '.join(_clip_ref_paths(clip, None))}")
    text = "\n".join(lines)
    if len(text) > 14000:
        text = text[:14000] + "\n[board truncated]"
    return text


async def _llm_plan(board: dict[str, Any]) -> dict[str, Any]:
    client = LLMClient.for_role("video", timeout=PLAN_TIMEOUT_SEC)
    try:
        raw = await client.chat(
            [
                {"role": "system", "content": _PLAN_SYSTEM},
                {
                    "role": "user",
                    "content": "Write the continuity ledger for this board.\n\n" + _board_digest(board),
                },
            ],
            temperature=0.2,
        )
        data = extract_json(raw)
    finally:
        await client.close()
    if not isinstance(data, dict):
        raise ValueError("continuity plan was not a JSON object")
    return data


async def ensure_continuity_plan(
    project_id: int,
    *,
    force: bool = False,
    live_refs: dict[int, list[str]] | None = None,
) -> dict[str, Any]:
    """Return the project's plan, rewriting it when the board hash is stale.

    A dead model falls back to a deterministic plan. The call never raises for
    model failures.
    """
    board = load_board(project_id)
    basis = basis_hash(board, live_refs)
    stored = _parse_stored(board["project"].get("continuity_json"))
    if stored and not force and stored.get("based_on") == basis:
        stored["refreshed"] = False
        return stored
    try:
        plan = normalize_plan(await _llm_plan(board), board)
    except Exception as exc:
        logger.warning("Continuity plan refresh failed (%s); using the board plan", exc)
        plan = deterministic_plan(board)
    plan["based_on"] = basis
    _persist(project_id, plan)
    plan["refreshed"] = True
    return plan


def continuity_lock_text(plan: dict[str, Any] | None, clip_id: int) -> str:
    """Binding slice for one clip: this shot plus its neighbors."""
    if not plan:
        return ""
    shots = [s for s in (plan.get("shots") or []) if isinstance(s, dict)]
    idx = next(
        (i for i, shot in enumerate(shots) if _as_int(shot.get("clip_id")) == int(clip_id)),
        None,
    )
    lines = [
        "These lines are binding.",
        "Phrase them into the six H3 sections.",
        "Do not recast a subject, move a dialogue line to another speaker, or change lighting or screen direction.",
        "Reference images and the reference video remain the identity and motion source.",
    ]
    overview = plan.get("overview") if isinstance(plan.get("overview"), dict) else {}
    for key in OVERVIEW_KEYS:
        val = str((overview or {}).get(key) or "").strip()
        if val:
            lines.append(f"Film {key}: {val}")
    req = plan.get("requirements") if isinstance(plan.get("requirements"), dict) else {}
    for key in REQUIREMENT_KEYS:
        val = str((req or {}).get(key) or "").strip()
        if val:
            lines.append(f"Locked {key}: {val}")
    if idx is None:
        lines.append("This clip has no shot row; keep the locks above.")
        return "\n".join(lines)

    def _fmt(label: str, shot: dict[str, Any]) -> str:
        return (
            f"{label} clip {shot.get('clip_id')} window {shot.get('window') or ''}: "
            f"action={shot.get('action') or ''}; speakers={shot.get('speakers') or ''}; "
            f"camera={shot.get('camera') or ''}; lighting={shot.get('lighting') or ''}; "
            f"spatial={shot.get('spatial') or ''}; after={shot.get('state_after') or ''}"
        )

    if idx > 0:
        lines.append(_fmt("Previous shot", shots[idx - 1]))
    lines.append(_fmt("This shot", shots[idx]))
    if idx + 1 < len(shots):
        lines.append(_fmt("Next shot", shots[idx + 1]))
    return "\n".join(lines)


def _unavailable(exc: BaseException) -> dict[str, Any]:
    reason = str(exc).strip() or exc.__class__.__name__
    return {
        "ok": False,
        "notes": [f"Continuity critic unavailable: {reason[:180]}"],
        "unavailable": True,
    }


async def critique_prompt(
    prompt: str, plan: dict[str, Any] | None, clip_id: int
) -> dict[str, Any]:
    """Score one candidate. Failures only. A dead judge never raises."""
    if not plan:
        return {"ok": True, "notes": []}
    client = LLMClient.for_role("video", timeout=CRITIC_TIMEOUT_SEC)
    try:
        raw = await client.chat(
            [
                {"role": "system", "content": _CRITIC_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        "Plan slice:\n"
                        + continuity_lock_text(plan, clip_id)
                        + "\n\nCandidate prompt:\n"
                        + (prompt or "")
                    ),
                },
            ],
            temperature=0.0,
        )
        data = extract_json(raw)
    except Exception as exc:
        logger.warning("Continuity critic failed (%s)", exc)
        return _unavailable(exc)
    finally:
        await client.close()
    if not isinstance(data, dict):
        return _unavailable(ValueError("critic returned no JSON object"))
    notes: list[str] = []
    for item in data.get("notes") or []:
        text = str(item).strip()
        if text:
            notes.append(text[:300])
    notes = notes[:9]
    ok = bool(data.get("ok")) and not notes
    return {"ok": ok, "notes": notes}
