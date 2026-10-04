"""Enqueue per-clip video generation jobs (a scene expands into many clips)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from calliope.agent.continuity import (
    continuity_lock_text,
    critique_prompt,
    ensure_continuity_plan,
    media_paths,
)
from calliope.agent.harness.log import (
    _image_attachment_data_url,
    _video_attachment_frames,
)
from calliope.agent.llm import LLMClient
from calliope.agent.prompts import (
    build_minimax_h3_ref_messages,
    minimax_h3_ref_fallback,
    scene_video_prompt,
)
from calliope.comfyui.parser import parse_dynamic_inputs
from calliope.comfyui.roles import input_has_role
from calliope.comfyui.smart_fill import (
    ref_audio_slots,
    ref_image_slots,
    ref_video_slots,
    smart_fill_inputs,
)
from calliope.config import settings
from calliope.db import get_db, row_to_dict
from calliope.events.bus import event_bus
from calliope.queue.manager import queue_manager

logger = logging.getLogger("calliope.video_agent")


def _path_key(path: str) -> str:
    try:
        return str(Path(path).resolve()).casefold()
    except OSError:
        return str(path).casefold()


def _paths_equal(a: str, b: str) -> bool:
    return bool(a and b) and _path_key(a) == _path_key(b)


def _form_media_path(values: dict[str, Any], node_id: Any) -> str | None:
    """A non-blank string the user stored on this workflow node."""
    raw = values.get(str(node_id))
    if raw is None:
        raw = values.get(node_id)
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def _story_image_roster(
    characters: list[dict[str, Any]],
    location: dict[str, Any] | None,
    loc_image: str | None,
) -> list[dict[str, Any]]:
    """Story images in fill order: scene characters, then the location.

    Used only for image slots the user left empty.
    """
    roster: list[dict[str, Any]] = []
    for c in characters:
        img = c.get("sheet_path") or c.get("portrait_path")
        if not img:
            continue
        roster.append(
            {
                "kind": "character",
                "name": c.get("name"),
                "appearance": c.get("consistency_prompt") or c.get("appearance") or "",
                "path": img,
            }
        )
    if loc_image:
        loc = location or {}
        roster.append(
            {
                "kind": "location",
                "name": loc.get("name"),
                "appearance": loc.get("consistency_prompt") or loc.get("description") or "",
                "path": loc_image,
            }
        )
    return roster


def resolve_h3_references(
    inputs: list[dict[str, Any]],
    values: dict[str, Any],
    characters: list[dict[str, Any]],
    location: dict[str, Any] | None,
    loc_image: str | None,
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
    """Image subjects, image paths, and video refs for an H3 prompt.

    Each ``(Input:image)`` slot uses the file the user put there. An empty
    slot falls back to the story image that would have filled that same index
    (characters, then location). Each ``(Input:video)`` slot the user filled
    becomes ``<Video N>``. Story text never replaces a file the user chose.
    """
    roster = _story_image_roster(characters, location, loc_image)
    subjects: list[dict[str, Any]] = []
    image_paths: list[str] = []
    for index, slot in enumerate(ref_image_slots(inputs)):
        user_path = _form_media_path(values, slot["nodeId"])
        if user_path:
            matched = next(
                (item for item in roster if _paths_equal(item["path"], user_path)),
                None,
            )
            if matched:
                subject = {
                    "kind": matched["kind"],
                    "name": matched.get("name"),
                    "appearance": matched.get("appearance") or "",
                    "path": user_path,
                }
            else:
                subject = {
                    "kind": "reference",
                    "name": Path(user_path).stem or "reference",
                    "appearance": "",
                    "path": user_path,
                }
        elif index < len(roster):
            item = roster[index]
            subject = {
                "kind": item["kind"],
                "name": item.get("name"),
                "appearance": item.get("appearance") or "",
                "path": item["path"],
            }
            user_path = item["path"]
        else:
            continue
        subject["index"] = len(subjects) + 1
        subjects.append(subject)
        image_paths.append(user_path)
    videos: list[dict[str, Any]] = []
    for slot in ref_video_slots(inputs):
        user_path = _form_media_path(values, slot["nodeId"])
        if not user_path:
            continue
        videos.append(
            {
                "index": len(videos) + 1,
                "path": user_path,
                "name": Path(user_path).name,
            }
        )
    return subjects, image_paths, videos


def resolve_ref_audios(inputs: list[dict[str, Any]], values: dict[str, Any]) -> list[str]:
    """Paths the user put in ``(Input:audio)`` slots, in node-id order.

    Unlike images and videos, audio has no story roster to fall back on: a slot
    is either filled with a file the user chose or it stays empty.
    """
    paths: list[str] = []
    for slot in ref_audio_slots(inputs):
        path = _form_media_path(values, slot["nodeId"])
        if path:
            paths.append(path)
    return paths


def _reference_signature(
    image_paths: list[str],
    video_paths: list[str],
    audio_paths: list[str] | None = None,
) -> str:
    """Fingerprint of the files a draft was written against. Empty when none."""
    parts: list[str] = []
    if image_paths:
        parts.append("img=" + ",".join(image_paths))
    if video_paths:
        parts.append("vid=" + ",".join(video_paths))
    if audio_paths:
        parts.append("aud=" + ",".join(audio_paths))
    return "|".join(parts)


async def _reference_media_parts(
    subjects: list[dict[str, Any]],
    videos: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Vision parts for the files under assets_dir. Missing files stay text-only."""
    parts: list[dict[str, Any]] = []
    for subject in subjects:
        path = str(subject.get("path") or "")
        url = _image_attachment_data_url(path)
        if not url:
            continue
        parts.append(
            {
                "type": "text",
                "text": (
                    f"Picture {subject['index']} is <Subject {subject['index']}>. "
                    "Describe the visible identity in this image."
                ),
            }
        )
        parts.append({"type": "image_url", "image_url": {"url": url}})
    for video in videos:
        frames = await asyncio.to_thread(_video_attachment_frames, str(video.get("path") or ""))
        if not frames:
            continue
        parts.append(
            {
                "type": "text",
                "text": (
                    f"<Video {video['index']}> frames, in time order. "
                    "Take camera, timing, and physical action from these frames."
                ),
            }
        )
        for ts, url in frames:
            parts.append(
                {"type": "text", "text": f"<Video {video['index']}> at {ts:.2f}s"}
            )
            parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


class _H3Compiler:
    """Per-batch H3 prompt compiler with a dead-endpoint circuit breaker.

    A dead LLM makes every clip fall back to the deterministic template after
    its own timeout — two minutes × N clips of dead air before anything renders.
    One failure is enough evidence that the endpoint is gone, so the breaker
    latches and every later clip goes straight to the template.

    One instance per batch. Sharing it across batches would suppress recovery
    after the endpoint comes back.
    """

    def __init__(self, *, timeout: float = 120.0) -> None:
        self.timeout = timeout
        self.dead = False

    async def rewrite(
        self,
        scene: dict[str, Any],
        subjects: list[dict[str, Any]],
        *,
        videos: list[dict[str, Any]] | None = None,
        continuity: str | None = None,
    ) -> str:
        videos = videos or []
        if self.dead:
            return minimax_h3_ref_fallback(scene, subjects, videos)
        client = LLMClient.for_role("video", timeout=self.timeout)
        try:
            return await client.chat(
                build_minimax_h3_ref_messages(
                    scene,
                    subjects,
                    videos=videos,
                    media_parts=(await _reference_media_parts(subjects, videos)) or None,
                    continuity=continuity,
                ),
                temperature=0.4,
                extra_body=settings.h3_rewrite_extra_body or None,
            )
        except Exception as exc:
            self.dead = True
            logger.warning(
                "MiniMax H3 prompt rewrite failed (%s); using fallback template "
                "for the rest of this batch too",
                exc,
            )
            return minimax_h3_ref_fallback(scene, subjects, videos)
        finally:
            await client.close()


async def _h3_rewrite(
    scene: dict[str, Any],
    subjects: list[dict[str, Any]],
    *,
    videos: list[dict[str, Any]] | None = None,
    continuity: str | None = None,
    timeout: float = 120.0,
) -> str:
    """LLM rewrite into H3's six-section format, deterministic template on failure.

    The timeout bounds the whole wait for a dead endpoint before the template
    kicks in — the preview path passes a short value so the UI fails fast.
    Reference images and video frames are attached when the files are readable
    under assets_dir; a text-only endpoint still receives the file roster.

    Single-shot. Batch callers hold one _H3Compiler so a dead endpoint costs one
    timeout for the batch rather than one per clip.
    """
    compiler = _H3Compiler(timeout=timeout)
    return await compiler.rewrite(scene, subjects, videos=videos, continuity=continuity)


def _video_input(inputs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """First workflow input whose canonical role is ``video``."""
    for inp in inputs:
        if input_has_role(inp, "video"):
            return inp
    return None


def _previous_clip(
    conn: sqlite3.Connection,
    project_id: int,
    global_position: tuple[int, int],
) -> tuple[str | None, bool, int | None]:
    """Nearest earlier clip (global playback order) + whether any earlier clip exists.

    ``global_position`` is the current clip's ``(scene.order_index, clip.order_index)``.
    Returns ``(path, has_earlier, clip_id)``; path is None when the file does not
    exist on disk (not yet generated) or there is no earlier clip. ``clip_id`` is
    returned alongside the path so the caller can tell an already-finished earlier
    clip from one this batch is about to re-render — the latter must be resolved
    at RUN time (its new file does not exist yet), not bound to the stale path.
    """
    scene_pos, clip_pos = global_position
    row = conn.execute(
        """
        SELECT c.id AS id, COALESCE(c.clip_path, s.video_path) AS clip_path
        FROM clips c
        JOIN scenes s ON s.id = c.scene_id
        WHERE c.project_id = ?
          AND (s.order_index < ? OR (s.order_index = ? AND c.order_index < ?))
          AND COALESCE(c.clip_path, s.video_path) IS NOT NULL
        ORDER BY s.order_index DESC, c.order_index DESC LIMIT 1
        """,
        (project_id, scene_pos, scene_pos, clip_pos),
    ).fetchone()
    has_earlier = conn.execute(
        """
        SELECT 1 FROM clips c JOIN scenes s ON s.id = c.scene_id
        WHERE c.project_id = ?
          AND (s.order_index < ? OR (s.order_index = ? AND c.order_index < ?))
        LIMIT 1
        """,
        (project_id, scene_pos, scene_pos, clip_pos),
    ).fetchone() is not None
    path = row["clip_path"] if row else None
    if path and Path(path).exists():
        return path, has_earlier, int(row["id"])
    return None, has_earlier, (int(row["id"]) if row else None)


def _fetch_clips(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    scene_ids: list[int] | None = None,
    clip_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Clips joined with their scenes, in global playback order.

    Clip columns COALESCE onto the scene's legacy columns (video_path, chain)
    so pre-clips rows and legacy scene-level writes keep working unchanged.
    """
    query = """
        SELECT c.*,
               COALESCE(c.clip_path, s.video_path) AS resolved_clip_path,
               CASE WHEN COALESCE(c.chain_from_prev, 0) = 0 AND c.order_index = 1
                    THEN COALESCE(s.chain_from_prev, 0)
                    ELSE COALESCE(c.chain_from_prev, 0) END AS chain_flag,
               s.heading, s.action, s.dialog, s.order_index AS scene_order_index,
               s.duration_sec AS scene_duration_sec, s.env_image_path, s.location_id
        FROM clips c JOIN scenes s ON s.id = c.scene_id
        WHERE c.project_id = ?
    """
    params: list[Any] = [project_id]
    if clip_ids:
        placeholders = ",".join("?" * len(clip_ids))
        query += f" AND c.id IN ({placeholders})"
        params.extend(clip_ids)
    elif scene_ids:
        placeholders = ",".join("?" * len(scene_ids))
        query += f" AND c.scene_id IN ({placeholders})"
        params.extend(scene_ids)
    query += " ORDER BY s.order_index, c.order_index, c.id"
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    for r in rows:
        # Legacy fallbacks: resolved names win over the raw clip columns.
        r["clip_path"] = r.get("resolved_clip_path") or r.get("clip_path")
        if r.get("chain_flag"):
            r["chain_from_prev"] = r["chain_flag"]
        r.pop("resolved_clip_path", None)
        r.pop("chain_flag", None)
    return rows


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


def _clip_video_settings(clip: dict[str, Any]) -> dict[str, Any]:
    """Parsed clips.video_settings_json — {} when unset or malformed."""
    raw = clip.get("video_settings_json")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _stored_input_values(clip: dict[str, Any]) -> dict[str, Any]:
    """Saved per-clip form values (input_values inside video_settings)."""
    values = _clip_video_settings(clip).get("input_values")
    if isinstance(values, dict):
        return {k: v for k, v in values.items() if v not in (None, "")}
    return {}


def _stored_prompt_draft(clip: dict[str, Any]) -> str | None:
    draft = _clip_video_settings(clip).get("prompt_draft")
    return draft if isinstance(draft, str) and draft.strip() else None


def _clip_prompt_hash(
    clip: dict[str, Any], *, references: str = "", ledger: str = ""
) -> str:
    """Cheap fingerprint of the inputs a draft was based on (stale detection).

    Combines the scene's content fingerprint with the clip's own fields, so an
    edit to either invalidates saved drafts. Keeps the scene-only fields in the
    basis (heading/action/dialog/location/characters) so legacy drafts saved
    against scene-level hashes invalidate consistently. ``references`` is the
    resolved image/video file signature — changing a ref on the clip form
    invalidates a draft that was written against different files. Empty
    references keep the historical hash. ``ledger`` is the continuity plan's
    ``based_on`` hash; a new plan invalidates a draft written against the old one.
    """
    basis = "|".join(
        str(clip.get(k) or "")
        for k in ("heading", "action", "dialog", "duration_sec", "location_id")
    )
    chars = ",".join(str(c) for c in sorted(clip.get("character_ids") or []))
    clip_basis = "|".join(
        str(clip.get(k) or "")
        for k in ("description", "shot_size", "duration_sec", "order_index")
    )
    payload = basis + "|" + chars + "|clip:" + clip_basis
    if references:
        payload += "|refs:" + references
    # Empty ledger keeps the historical hash so drafts saved before a plan
    # existed still match until a plan hash is actually stored.
    if ledger:
        payload += "|ledger:" + ledger
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _merged_form_values(
    clip: dict[str, Any], override: dict[str, Any] | None
) -> dict[str, Any]:
    """Saved clip form values, with the live request winning on each node."""
    values = dict(_stored_input_values(clip))
    if override:
        values.update({k: v for k, v in override.items() if v not in (None, "")})
    return values


async def _critique_or_unavailable(
    prompt: str, plan: dict[str, Any] | None, clip_id: int
) -> dict[str, Any]:
    """Critic result. A raised judge becomes ok=false, never a 500."""
    try:
        return await critique_prompt(prompt, plan, clip_id)
    except Exception as exc:
        logger.warning("Continuity critic failed (%s)", exc)
        reason = str(exc).strip() or exc.__class__.__name__
        return {
            "ok": False,
            "notes": [f"Continuity critic unavailable: {reason[:180]}"],
            "unavailable": True,
        }


async def preview_clip_prompt(
    project_id: int,
    clip_id: int,
    workflow_id: int | None = None,
    input_values: dict[str, Any] | None = None,
    force: bool = False,
    compiler: _H3Compiler | None = None,
    with_critic: bool = True,
) -> dict[str, Any]:
    """Resolve the exact prompt a Generate would send — without enqueueing.

    Returns {"prompt", "profile", "from_draft", "based_on"} and, for an H3
    workflow, "critic": {"ok", "notes"}. The critic never blocks Generate.

    ``compiler`` shares one _H3Compiler across a batch so a dead endpoint costs
    a single timeout. ``with_critic=False`` skips the continuity judge — it is a
    human-review aid and doubles the LLM calls, so batch compilation drops it.
    """
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            """
            SELECT c.*, s.heading, s.action, s.dialog, s.order_index AS scene_order_index,
                   s.env_image_path, s.location_id
            FROM clips c JOIN scenes s ON s.id = c.scene_id
            WHERE c.id = ? AND c.project_id = ?
            """,
            (clip_id, project_id),
        ).fetchone()
        if not row:
            raise ValueError(f"Clip {clip_id} not found in project {project_id}")
        clip = dict(row)
        scene_id = clip["scene_id"]
        char_rows = conn.execute(
            """
            SELECT c.* FROM characters c
            JOIN scene_characters sc ON sc.character_id = c.id
            WHERE sc.scene_id = ?
            """,
            (scene_id,),
        ).fetchall()
        characters = [row_to_dict(r) for r in char_rows]
        loc_image = clip.get("env_image_path")
        loc_row: dict[str, Any] | None = None
        if clip.get("location_id"):
            loc = conn.execute(
                "SELECT name, description, consistency_prompt, reference_image_path "
                "FROM locations WHERE id = ?",
                (clip["location_id"],),
            ).fetchone()
            if loc:
                loc_row = row_to_dict(loc)
                if not loc_image:
                    loc_image = loc_row["reference_image_path"]
    finally:
        conn.close()

    scene = _scene_fields_from_clip(clip, characters)
    wf_id = workflow_id or clip.get("workflow_id")
    workflow = _get_workflow(wf_id)
    if not workflow:
        raise ValueError("No enabled video workflow found — configure one in Settings")
    inputs = parse_dynamic_inputs(_workflow_json(workflow))
    profile = workflow.get("prompt_profile") or "prose"
    form_values = _merged_form_values(clip, input_values)
    subjects, _image_paths, videos = resolve_h3_references(
        inputs, form_values, characters, loc_row, loc_image
    )
    ref_audios = resolve_ref_audios(inputs, form_values)
    ref_sig = _reference_signature(
        _image_paths, [v["path"] for v in videos], ref_audios
    )
    hash_clip = {**clip, "character_ids": [c["id"] for c in characters]}

    if profile == "minimax_h3_ref":
        plan = await ensure_continuity_plan(
            project_id, live_refs={clip_id: media_paths(form_values)}
        )
        ledger = str(plan.get("based_on") or "")
        based_on = _clip_prompt_hash(hash_clip, references=ref_sig, ledger=ledger)
        lock = continuity_lock_text(plan, clip_id)
        # A fresh draft skips the compiler. The critic still runs so the modal
        # can show continuity notes. Regenerate passes force=True.
        draft = _stored_prompt_draft(clip)
        from_draft = False
        meta = _clip_video_settings(clip).get("prompt_draft_meta") or {}
        if draft and not force and meta.get("based_on") == based_on:
            prompt = draft
            from_draft = True
        else:
            await event_bus.publish(
                "agent.thinking",
                {"message": f"H3 prompt rewrite · scene {clip.get('scene_order_index')} clip {clip.get('order_index')}", "project_id": project_id},
            )
            # Preview is interactive — fail fast to the deterministic template
            # instead of making the user wait out a dead endpoint. A batch shares
            # one compiler, whose breaker keeps the first timeout from repeating.
            writer = compiler or _H3Compiler(timeout=30.0)
            prompt = await writer.rewrite(scene, subjects, videos=videos, continuity=lock)
        if with_critic:
            verdict = await _critique_or_unavailable(prompt, plan, clip_id)
            # A dead or unreadable judge leaves this prompt in place and reports
            # the note. The rewrite already falls back to the template when that
            # call itself fails.
            critic_out: dict[str, Any] = {
                "ok": bool(verdict.get("ok")),
                "notes": list(verdict.get("notes") or []),
            }
        else:
            critic_out = {"ok": True, "notes": []}
        return {
            "prompt": prompt,
            "profile": profile,
            "from_draft": from_draft,
            "based_on": based_on,
            "critic": critic_out,
        }

    based_on = _clip_prompt_hash(hash_clip, references=ref_sig)
    prompt = scene_video_prompt(scene, characters)
    return {"prompt": prompt, "profile": profile, "from_draft": False, "based_on": based_on}


def _save_prompt_draft(clip_id: int, prompt: str, based_on: str) -> None:
    """Persist a compiled prompt as the clip's draft so Generate spends no LLM.

    Writes through video_settings_json rather than the generic clips patch so
    the compiled text and the fingerprint it was compiled against land in the
    same statement — a draft without its `based_on` reads as stale forever.
    """
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT video_settings_json FROM clips WHERE id = ?", (clip_id,)
        ).fetchone()
        if not row:
            return
        try:
            data = json.loads(row["video_settings_json"] or "{}")
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        data["prompt_draft"] = prompt
        data["prompt_draft_meta"] = {"based_on": based_on}
        conn.execute(
            "UPDATE clips SET video_settings_json = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False), clip_id),
        )
        conn.commit()
    finally:
        conn.close()


async def rewrite_clip_prompts(
    project_id: int,
    *,
    clip_ids: list[int] | None = None,
    only_missing: bool = False,
    workflow_id: int | None = None,
    input_values: dict[str, Any] | None = None,
    force: bool = True,
    save: bool = True,
) -> dict[str, Any]:
    """Compile H3 prompts for many clips up front, queueing no render.

    The point is VRAM, not throughput. `enqueue_video_jobs` now uses the same
    shape — every prompt first, then the job rows — so the LLM owns the GPU for
    the whole compile and ComfyUI only starts once the last prompt exists.

    ``clip_ids`` restricts the work to specific clips (recompile one shot);
    ``only_missing`` skips clips that already carry a draft. Results are saved
    with their fingerprint, so the Generate that follows costs zero LLM calls.

    Sequential on purpose: parallel rewrites would hold N prompts in flight
    against one llama.cpp router and simply queue inside it.
    """
    conn = get_db(settings.db_path)
    try:
        clips = _fetch_clips(conn, project_id, clip_ids=clip_ids)
    finally:
        conn.close()

    compiler = _H3Compiler(timeout=120.0)
    results: list[dict[str, Any]] = []
    skipped = 0
    total = len(clips)
    for index, clip in enumerate(clips, start=1):
        label = _clip_label(clip)
        if only_missing and _stored_prompt_draft(clip):
            skipped += 1
            continue
        await event_bus.publish(
            "agent.thinking",
            {
                "message": f"H3 prompt compile · {label} ({index}/{total})",
                "project_id": project_id,
            },
        )
        try:
            item = await preview_clip_prompt(
                project_id,
                int(clip["id"]),
                workflow_id=workflow_id,
                input_values=input_values,
                force=force,
                compiler=compiler,
                with_critic=False,
            )
        except ValueError as exc:
            results.append(
                {"clip_id": int(clip["id"]), "label": label, "ok": False, "error": str(exc)}
            )
            continue
        persisted = save and item["profile"] == "minimax_h3_ref"
        if persisted:
            _save_prompt_draft(int(clip["id"]), item["prompt"], item["based_on"])
        results.append(
            {
                "clip_id": int(clip["id"]),
                "label": label,
                "ok": True,
                "prompt": item["prompt"],
                "profile": item["profile"],
                "from_draft": item["from_draft"],
                "saved": persisted,
            }
        )

    return {
        "results": results,
        "skipped": skipped,
        "total": total,
        # True means every remaining clip came from the deterministic template,
        # not the LLM — the caller should say so rather than look successful.
        "endpoint_dead": compiler.dead,
        "compiled": sum(1 for r in results if r.get("ok") and not r.get("from_draft")),
        "failed": sum(1 for r in results if not r.get("ok")),
    }


def _scene_fields_from_clip(
    clip: dict[str, Any], characters: list[dict[str, Any]]
) -> dict[str, Any]:
    """A scene-shaped dict whose action/dialog are narrowed to the clip's beat.

    Both the prose and H3 prompt builders consume scene rows; feeding them this
    projection means a clip's prompt contains only what that clip performs.
    """
    desc = (clip.get("description") or "").strip()
    action = desc or (clip.get("action") or "").strip()
    if desc and clip.get("shot_size"):
        action = f"{clip['shot_size']} shot. {action}"
    dialog = _clip_dialog(clip)
    return {
        "heading": clip.get("heading"),
        "action": action,
        "dialog": dialog,
        "duration_sec": clip.get("duration_sec"),
        "character_ids": [c["id"] for c in characters],
    }


def _clip_dialog(clip: dict[str, Any]) -> str | None:
    """The dialog lines this clip performs (from dialog_lines_covered), verbatim."""
    covered = clip.get("dialog_lines_covered")
    if not covered:
        return None
    if isinstance(covered, str):
        # Raw DB column is a JSON array string — parse before indexing.
        try:
            covered = json.loads(covered)
        except (json.JSONDecodeError, TypeError):
            return None
    idxs = covered if isinstance(covered, list) else []
    lines = [ln for ln in (clip.get("dialog") or "").splitlines() if ln.strip()]
    picked = [lines[i - 1] for i in idxs if isinstance(i, int) and 1 <= i <= len(lines)]
    return "\n".join(picked) or None


async def enqueue_video_jobs(
    project_id: int,
    *,
    scene_ids: list[int] | None = None,
    clip_ids: list[int] | None = None,
    workflow_id: int | None = None,
    input_values_override: dict[str, Any] | None = None,
    prompts: dict[int, str] | None = None,
    session_id: int | None = None,
) -> list[dict[str, Any]]:
    await event_bus.publish(
        "agent.thinking", {"message": "Queuing video jobs…", "project_id": project_id}
    )
    conn = get_db(settings.db_path)
    jobs: list[dict[str, Any]] = []
    continuity_plan: dict[str, Any] | None = None
    compiler = _H3Compiler()
    try:
        clips = _fetch_clips(conn, project_id, scene_ids=scene_ids, clip_ids=clip_ids)
        batch_ids = {int(c["id"]) for c in clips}

        # ── PASS 1 · prompts only ────────────────────────────────────────────
        # Every H3 rewrite is an LLM round-trip, and writing a job row hands that
        # clip to the queue worker (queue/worker.py, 2 s poll) at once. Doing both
        # in one loop meant ComfyUI was already holding the H3 weights in VRAM
        # while the next rewrite asked llama.cpp for the LLM — on a single GPU
        # with --models-max 1 that is a guaranteed stall or OOM. Compiling the
        # whole batch before any row exists keeps the worker idle, so the LLM
        # has the card to itself.
        #
        # This pass writes NOTHING: a batch that dies mid-compile leaves the
        # previous renders intact (the destructive writes live in pass 2).
        pending: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        for clip in clips:
            scene_id = clip["scene_id"]

            wf_id = workflow_id or clip.get("workflow_id")
            workflow = _get_workflow(wf_id)

            workflow_json = _workflow_json(workflow)
            inputs = parse_dynamic_inputs(workflow_json) if workflow_json else []
            duration = clip.get("duration_sec") or clip.get("scene_duration_sec")

            char_rows = conn.execute(
                """
                SELECT c.* FROM characters c
                JOIN scene_characters sc ON sc.character_id = c.id
                WHERE sc.scene_id = ?
                """,
                (scene_id,),
            ).fetchall()
            characters = [row_to_dict(r) for r in char_rows]
            char_ids = [c["id"] for c in characters]
            char_image = next(
                (c.get("sheet_path") or c.get("portrait_path") for c in characters if c.get("sheet_path") or c.get("portrait_path")),
                None,
            )
            loc_image = clip.get("env_image_path")
            loc_row: dict[str, Any] | None = None
            if clip.get("location_id"):
                row = conn.execute(
                    "SELECT name, description, consistency_prompt, reference_image_path "
                    "FROM locations WHERE id = ?",
                    (clip["location_id"],),
                ).fetchone()
                if row:
                    loc_row = row_to_dict(row)
                    if not loc_image:
                        loc_image = loc_row["reference_image_path"]

            scene = _scene_fields_from_clip(clip, characters)
            profile = (workflow or {}).get("prompt_profile") or "prose"
            hash_input = {**clip, "character_ids": char_ids}
            # Merge base: saved per-clip setup first, explicit request wins on
            # top — so batch Generate-all honors persisted form setups.
            stored_values = _stored_input_values(clip)
            extra_values: dict[str, Any] = {**stored_values}
            if input_values_override:
                extra_values.update(
                    {k: v for k, v in input_values_override.items() if v not in (None, "")}
                )
            if profile == "minimax_h3_ref":
                if continuity_plan is None:
                    live_refs = None
                    if input_values_override:
                        live_refs = {
                            int(c["id"]): media_paths(
                                {
                                    **_stored_input_values(c),
                                    **{
                                        k: v
                                        for k, v in input_values_override.items()
                                        if v not in (None, "")
                                    },
                                }
                            )
                            for c in clips
                        }
                    continuity_plan = await ensure_continuity_plan(
                        project_id, live_refs=live_refs
                    )
                subjects, ref_paths, videos = resolve_h3_references(
                    inputs, extra_values, characters, loc_row, loc_image
                )
                ref_audios = resolve_ref_audios(inputs, extra_values)
                ref_sig = _reference_signature(
                    ref_paths, [v["path"] for v in videos], ref_audios
                )
                ledger = str((continuity_plan or {}).get("based_on") or "")
                lock = continuity_lock_text(continuity_plan, int(clip["id"]))
                # Prompt precedence: explicit request → saved (fresh) draft → LLM.
                # Batch enqueue compiles from the ledger and does not run the critic.
                explicit_prompt = (prompts or {}).get(clip["id"])
                fresh_draft = None
                if explicit_prompt is None:
                    candidate = _stored_prompt_draft(clip)
                    if candidate:
                        meta = _clip_video_settings(clip).get("prompt_draft_meta") or {}
                        if meta.get("based_on") == _clip_prompt_hash(
                            hash_input, references=ref_sig, ledger=ledger
                        ):
                            fresh_draft = candidate
                if explicit_prompt is not None:
                    prompt = explicit_prompt
                elif fresh_draft:
                    prompt = fresh_draft
                else:
                    await event_bus.publish(
                        "agent.thinking",
                        {
                            "message": f"H3 prompt rewrite · scene {clip.get('scene_order_index')} clip {clip.get('order_index')}",
                            "project_id": project_id,
                        },
                    )
                    prompt = await compiler.rewrite(
                        scene, subjects, videos=videos, continuity=lock
                    )
                values = smart_fill_inputs(
                    inputs,
                    prompt=prompt,
                    ref_images=ref_paths,
                    ref_videos=[v["path"] for v in videos],
                    ref_audios=ref_audios,
                    duration=duration,
                    extra=extra_values,
                )
            else:
                prompt = (prompts or {}).get(clip["id"]) or scene_video_prompt(
                    scene, characters
                )
                values = smart_fill_inputs(
                    inputs,
                    prompt=prompt,
                    character_image=char_image,
                    location_image=loc_image,
                    duration=duration,
                    extra=extra_values,
                )
            # Stored per-clip setups override smart-fill's context choices
            # (e.g. an edited duration). smart_fill skips duration-role nodes
            # in `extra` by design — re-apply them here, request values win.
            explicit_final = {
                **stored_values,
                **{k: v for k, v in (input_values_override or {}).items() if v not in (None, "")},
            }
            for k, v in explicit_final.items():
                if v not in (None, ""):
                    values[str(k)] = v
            payload: dict[str, Any] = {"input_values": values, "prompt": prompt}
            if session_id is not None:
                payload["session_id"] = session_id
            if clip.get("chain_from_prev"):
                video_input = _video_input(inputs)
                if not video_input:
                    # Skip, don't abort: one clip pointed at a workflow without
                    # (Input:video) must not cost the batch its other renders.
                    skipped.append(
                        {
                            "clip_id": int(clip["id"]),
                            "label": _clip_label(clip),
                            "error": (
                                f"Clip {clip.get('scene_order_index')}.{clip.get('order_index')} "
                                f"is marked continue-from-previous but workflow "
                                f"'{(workflow or {}).get('name') or workflow_id}' has no "
                                "video input — pick a workflow with a (Input:video) node."
                            ),
                        }
                    )
                    continue
                video_node_id = str(video_input["nodeId"])
                if not values.get(video_node_id):
                    # Explicit clip from the form / input_values_override wins.
                    prev_clip, has_earlier, prev_id = _previous_clip(
                        conn,
                        project_id,
                        (clip.get("scene_order_index") or 0, clip.get("order_index") or 0),
                    )
                    # An earlier clip IN THIS BATCH still holds its previous
                    # render, because pass 1 no longer NULLs clip_path up front.
                    # Binding to that file would chain the new clip to a stale
                    # video — the earlier clip is about to be re-rendered, so
                    # defer to the worker instead (same reason as below).
                    if prev_clip and prev_id is not None and prev_id not in batch_ids:
                        # Local path: the worker's prepare_media_inputs uploads it
                        # to ComfyUI before queuing (same shape as char/loc refs).
                        values[video_node_id] = prev_clip
                    elif has_earlier:
                        # Previous clip not generated yet (typical when a batch is
                        # queued in one go). The worker resolves it at RUN time —
                        # the queue is concurrency-1, so the earlier clip
                        # will exist by then.
                        payload["continue_source"] = {
                            "scene_order_index": clip.get("scene_order_index"),
                            "clip_order_index": clip.get("order_index"),
                        }
                    else:
                        # Distinct from the missing-video-node case above: this is
                        # a project-level mistake (nothing earlier to chain
                        # from), so the whole batch is meaningless — raise rather
                        # than return an empty queue with only a log line.
                        raise ValueError(
                            f"Clip {clip.get('scene_order_index')}.{clip.get('order_index')} is marked "
                            "continue-from-previous but no previous clip exists yet — generate an "
                            "earlier clip first or upload a video in the Video stage."
                        )
            pending.append(
                {
                    "clip": clip,
                    "scene_id": scene_id,
                    "workflow": workflow,
                    "payload": payload,
                }
            )

        # ── PASS 2 · enqueue ────────────────────────────────────────────────
        # Every prompt is compiled, so the first row below can only be seen by
        # the worker once the LLM is done with the GPU.
        for item in pending:
            clip = item["clip"]
            scene_id = item["scene_id"]
            workflow = item["workflow"]
            # Supersede leftover pending jobs so a re-Generate always starts fresh
            conn.execute(
                """
                UPDATE jobs SET status = 'failed', error = 'superseded by new generate',
                completed_at = CURRENT_TIMESTAMP
                WHERE clip_id = ? AND kind = 'video' AND status = 'pending'
                """,
                (clip["id"],),
            )
            # Clear prior render so UI shows generating instead of the old video.
            # Also NULL the scene's legacy mirror — COALESCE reads fall back to
            # it, so a stale path there would resurrect the old render.
            conn.execute(
                "UPDATE clips SET clip_path = NULL WHERE id = ?",
                (clip["id"],),
            )
            conn.execute(
                "UPDATE scenes SET video_path = NULL WHERE id = ?",
                (scene_id,),
            )
            if workflow and not clip.get("workflow_id"):
                conn.execute(
                    "UPDATE clips SET workflow_id = ? WHERE id = ?",
                    (workflow["id"], clip["id"]),
                )
            # Commit BEFORE enqueue: queue_manager.enqueue writes on its own
            # connection, and holding this transaction open across that call
            # self-deadlocks (sqlite3.OperationalError: database is locked).
            conn.commit()
            job = queue_manager.enqueue(
                project_id=project_id,
                kind="video",
                workflow_id=workflow["id"] if workflow else None,
                scene_id=scene_id,
                clip_id=clip["id"],
                payload=item["payload"],
            )
            conn.commit()
            jobs.append(job)
            await event_bus.publish(
                "job.created",
                {
                    "job_id": job["id"],
                    "kind": "video",
                    "message": _clip_label(clip),
                    "project_id": project_id,
                },
            )
        conn.commit()
        if skipped:
            # Silent partial batches are the worst outcome: the operator sees
            # "Generate" succeed and only later notices a clip never rendered.
            for item in skipped:
                logger.warning("Video job skipped · %s · %s", item["label"], item["error"])
            logger.warning(
                "Video batch enqueued %d of %d clips; %d skipped (see warnings above)",
                len(jobs),
                len(jobs) + len(skipped),
                len(skipped),
            )
    finally:
        conn.close()
    return jobs


def _clip_label(clip: dict[str, Any]) -> str:
    """Human label: '#<scene>.<clip> · <heading>' (scene heading retained)."""
    heading = (clip.get("heading") or f"Scene {clip.get('scene_order_index')}").strip()
    return f"#{clip.get('scene_order_index')}.{clip.get('order_index')} · {heading}"
