"""System prompt composition — a section registry, assembled per step.

Ported from the deepseek-harness shape: the system prompt is a list of
registered sections (key, order, render) sorted by order and joined.
One broken section must never kill the prompt.

The tool-discipline section is deliberately LEAN. This is a local harness
for a local user: the rules teach the ReAct cycle (look → act → observe →
decide → finish) and get out of the way. The user's message is the plan;
permissions flow from it; guards exist to catch mistakes, not to run the
show. What killed canvas/70 was an agent that got a guard denial and then
ground in circles for ten minutes — the rules below make escalation and
finishing first-class moves instead.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from calliope.agent.harness.registry import ToolContext, _db, _project_stats
from calliope.config import settings
from calliope.db import row_to_dict

logger = logging.getLogger("calliope.harness.prompts")


@dataclass
class PromptSection:
    """One system-prompt contribution. `render` may return None to skip."""

    key: str
    order: int  # ascending; ties keep registration order
    render: Callable[[ToolContext], Awaitable[str | None]]
    seq: int = field(default=0)  # registration-order tiebreaker


class SystemPromptService:
    def __init__(self) -> None:
        self._sections: list[PromptSection] = []
        self._seq = 0

    def register(
        self,
        key: str,
        order: int,
        render: Callable[[ToolContext], Awaitable[str | None]],
    ) -> None:
        self._seq += 1
        self._sections.append(
            PromptSection(key=key, order=order, render=render, seq=self._seq)
        )
        self._sections.sort(key=lambda s: (s.order, s.seq))

    async def assemble(self, ctx: ToolContext) -> str:
        parts: list[str] = []
        for section in self._sections:
            try:
                text = await section.render(ctx)
            except Exception:
                logger.exception("Prompt section %r failed; skipping", section.key)
                continue
            if text:
                parts.append(text.strip())
        return "\n\n".join(p for p in parts if p)


# ─────────────────────────────────────────────────────────────────────────
# Built-in sections
# ─────────────────────────────────────────────────────────────────────────


async def _persona_section(ctx: ToolContext) -> str | None:
    return _persona_text()


def _persona_text() -> str:
    return (
        "You are Calliope's production agent — an AI assistant that builds and "
        "edits short-film / music-video projects end-to-end through tools."
    )


async def _mode_section(ctx: ToolContext) -> str | None:
    return _mode_text(ctx)


def _mode_text(ctx: ToolContext) -> str:
    if getattr(ctx, "origin", "chat") == "scene":
        return (
            "SESSION MODE: BUILD SCENE — this chat drives the 3D composition.\n"
            "- One surface: the Three.js viewport (the 3D editor) + TimelineStrip "
            "+ Export video. The viewport is the ONLY preview and the ONLY "
            "export/capture source — there is no separate render preview.\n"
            "- Tools here are shot_* + ask_user + skills/memory ONLY. "
            "ComfyUI / run_workflow / list_workflows / enqueue_* are "
            "unavailable and must not be attempted.\n"
            "- Export video / stills come from the 3D editor viewport (the user "
            "clicks Capture / Export video). There are no camera-beat tools.\n"
            "- NEVER call a tool named export_video (it does not exist). After "
            "Cut (ask_user → record_build_scene_gate), tell the user to click "
            "**Export video** in the UI. Stills: request_capture only.\n"
            "- Prefer read_skill(\"shot-composer-blockout\"); do NOT follow "
            "scene-to-video / enqueue_video_jobs for Build Scene.\n"
            "- Do not create_project just to animate — stay on this composition."
        )
    if ctx.project_id is None:
        return (
            "SESSION MODE: SANDBOX — no project is linked yet.\n"
            "- You can brainstorm and discuss ideas freely.\n"
            "- GENERATING IN SANDBOX (either path):\n"
            "  • Tagged @workflow + the user asked to generate: call "
            "run_workflow with that workflow_id (Playground scratch).\n"
            "  • NO tag + the user asks for an image/video: list_workflows, "
            "pick the enabled workflow that best matches the request (when "
            "ambiguous or none is obvious, ask_user which to use), then "
            "run_workflow with the user's prompt. You do NOT need a tag to "
            "generate — the tag is just a shortcut that names the workflow "
            "for you.\n"
            "- AFTER a generation completes: the result auto-appears on this "
            "session's sandbox canvas; you can also post_artifact_to_canvas("
            "job_id) to re-post or title it. This is the default finishing "
            "move — the user is watching that board.\n"
            "- The sandbox canvas is a real canvas: image/video cards land "
            "there and stay for comparison. It is NOT project-only.\n"
            "- Only file a generated image onto an existing film when the "
            "user EXPLICITLY names one (list_projects, then attach_asset). "
            "Never attach to a project just to make an image 'visible' — "
            "the sandbox canvas already shows it.\n"
            "- When the user wants a NEW film (story/script/assets): call "
            "create_project with a clear title + idea. That links the session.\n"
            "- To work on an EXISTING project: list_projects then link_project. "
            "NEVER create a duplicate when one already matches.\n"
            "- Note: create_project / link_project / list_projects only work "
            "while in sandbox mode; once linked they disappear."
        )
    return (
        f"SESSION MODE: LINKED — you are working inside project #{ctx.project_id}.\n"
        "- Every tool automatically operates on THIS project only; you "
        "cannot see or touch other projects, and you never need to pass a "
        "project id.\n"
        "- scene_id / character_id / location_id / job_id / workflow_id are "
        "REAL numeric ids from tool results — never invent them.\n"
        "- Video page #N is order (clip number), NOT scene_id. list_scenes "
        "returns clip=#N, order, and scene_id. Users say 'scene 25' / '#25' "
        "meaning order=25 — pass enqueue_video_jobs.orders=[25] or "
        "update_scene.order=25. Never treat a # as a database id.\n"
        "- To file a generated image onto a character, environment, or item: "
        "attach_asset with job_id (or path) and a name or entity id.\n"
        "- Film clips: enqueue_video_jobs with refs/clip_ids/orders for ONLY "
        "the clips they named. Never dump every scene_id. Never omit the "
        "list (that is not 'all'). all_clips=true only if they said all/"
        "every clip. Orphan video jobs (wired_to_scene=false) use "
        "attach_asset target=scene + scene_id.\n"
        "- Clip versioning ('apply render 430 to scene 2', 'use the older "
        "take'): attach_asset target=scene with that job_id + scene_id — "
        "the same Apply-to-Scene the Video page's render history offers. "
        "Each re-render of a scene keeps its old job outputs; list_jobs "
        "shows them."
    )


async def _workspace_digest_section(ctx: ToolContext) -> str | None:
    return (
        "CURRENT WORKSPACE STATE (may change between turns — refresh with "
        "get_workspace when you need fresh data):\n"
        f"{workspace_digest(ctx)}"
    )


def workspace_digest(ctx: ToolContext) -> str:
    """Compact snapshot of the session's project for the system prompt."""
    if ctx.project_id is None:
        return "Sandbox — no project data yet."
    conn = _db()
    try:
        row = conn.execute(
            "SELECT id, title, idea, genre, tone, target_duration, status "
            "FROM projects WHERE id = ?",
            (ctx.project_id,),
        ).fetchone()
        if not row:
            return "Linked project no longer exists."
        p = row_to_dict(row)
        stats = _project_stats(ctx.project_id, conn)
        beats = conn.execute(
            "SELECT COUNT(*) AS n FROM story_beats WHERE project_id = ?", (ctx.project_id,)
        ).fetchone()["n"]
        chars = conn.execute(
            "SELECT id, name, sheet_path FROM characters WHERE project_id = ?",
            (ctx.project_id,),
        ).fetchall()
        locs = conn.execute(
            "SELECT id, name, reference_image_path FROM locations WHERE project_id = ?",
            (ctx.project_id,),
        ).fetchall()
        scenes = conn.execute(
            "SELECT id, order_index, heading, video_path FROM scenes "
            "WHERE project_id = ? ORDER BY order_index",
            (ctx.project_id,),
        ).fetchall()
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE project_id = ? AND status IN ('pending','running')",
            (ctx.project_id,),
        ).fetchone()["n"]
        return _render_digest(p, stats, beats, chars, locs, scenes, pending)
    finally:
        conn.close()


def _render_digest(p, stats, beats, chars, locs, scenes, pending) -> str:
    lines = [
        f"project: #{p['id']} \"{p['title']}\" [{p['status']}]"
        + (f" — {p['idea']}" if p.get("idea") else ""),
        f"genre: {p.get('genre') or '-'} | tone: {p.get('tone') or '-'} | "
        f"target: {p.get('target_duration') or '-'}",
        f"counts: {stats['scene_count']} scenes, {stats['character_count']} characters, "
        f"{stats['asset_ready_count']}/{stats['asset_total_count']} assets ready, "
        f"{beats} beats, {pending} jobs in flight",
    ]
    if chars:
        ready = " ".join(f"#{c['id']}{'✓img' if c['sheet_path'] else '✗img'}" for c in chars)
        lines.append(f"characters: {ready}")
    if locs:
        ready = " ".join(
            f"#{l['id']}{'✓img' if l['reference_image_path'] else '✗img'}" for l in locs
        )
        lines.append(f"locations: {ready}")
    if scenes:
        s = " ".join(
            f"#{sc['order_index']} id={sc['id']}:'{sc['heading']}'"
            f"{'✓vid' if sc['video_path'] else '✗vid'}"
            for sc in scenes[:15]
        )
        lines.append(f"scenes: {s}" + (" …" if len(scenes) > 15 else ""))
    return "\n".join(lines)


async def _tool_discipline_section(ctx: ToolContext) -> str | None:
    return (
        "You work in a ReAct loop: each step you may call tools, see their "
        "results, and decide the next move. Multi-step is normal — one tool "
        "call per step, observe the result, then reason and act again. The "
        "user's message is the plan; execute it.\n"
        "1. ACT on the request. If a tool matches what the user asked, call "
        "it — don't re-read state you already have, don't ask what has "
        "already been answered. One get_workspace at the START of a turn is "
        "plenty; the digest above is usually enough.\n"
        "2. OBSERVE. Tool results tell you what happened. A result with "
        "ok:false is information, not a wall: read the error, fix the "
        "arguments or pick a different tool. NEVER repeat the exact same "
        "call expecting a different result — after one failed attempt, "
        "either CHANGE the approach (different args / different tool) or "
        "hand control back to the user.\n"
        "3. ESCALATE, don't spin. If you are blocked twice on the same "
        "goal, or a guard denies you, stop retrying: tell the user exactly "
        "what was denied and why, then either ask_user (question card) or "
        "end the turn with a plain explanation. Two failed attempts = "
        "report, not retry.\n"
        "4. FINISH cleanly. When the goal is met, reply with a concise "
        "summary of what was created/changed (ids, job numbers) and NO "
        "tool call. Ending the turn with an answer is always allowed.\n"
        "5. GUARDS: destructive tools (generate_story / generate_script "
        "replace=true) are blocked only when the user did NOT ask for the "
        "replacement — if they asked (e.g. 'regenerate the script'), the "
        "guard allows it and you should just retry; if they didn't, "
        "ask_user first, then retry once they confirm. Renders "
        "(enqueue_*, run_workflow) need the user to have asked for "
        "generation in their own words; render tools are hidden until "
        "then. A guard denial names the reason — act on it once, don't "
        "loop.\n"
        "6. SCOPE from args, never from vibes: pass the ids/refs the user "
        "named (orders=[3], clip_ids=[12], character_ids=[5]); bulk flags "
        "(all_missing / all_clips) only when they said all/every/"
        "remaining. Bulk enqueues above 3 targets are refused without "
        "such a word — that gate is about size, not permission.\n"
        "7. #N is ORDER (clip number), never a database id. IDs come only "
        "from tool results. Never fabricate ids or results.\n"
        "8. MEMORY: save_memory the moment the user states a durable "
        "preference or corrects a recurring behavior (one atomic "
        "sentence; list first, forget stale contradictions). Do not save "
        "one-off task details."
    )


async def _mentions_section(ctx: ToolContext) -> str | None:
    if getattr(ctx, "origin", "chat") == "scene":
        return (
            "Build Scene sessions ignore workflow tags for generation. "
            "Do not call run_workflow / ComfyUI from this chat. A tagged "
            "workflow_id= appendix is context only — the 3D editor viewport "
            "and shot_* tools own this surface. ComfyUI belongs to explicit "
            "image/video gen asks outside Build Scene."
        )
    if ctx.project_id is None:
        return (
            "Tagged workflows: a [Calliope context] appendix with workflow_id= "
            "names the workflow the user picked — prefer it for run_workflow. "
            "With NO tag you may still generate in sandbox: list_workflows, "
            "choose the best enabled match (ask_user when ambiguous), then "
            "run_workflow. A tag is a shortcut, not a permission: render "
            "approval comes only from the user's own words asking to "
            "generate, or a question-card approval. One tagged workflow per "
            "message (first id wins). Sandbox: do not create_project just to "
            "generate. Video file attachments are context only (the worker "
            "uploads image + audio refs)."
        )
    return (
        "Tagged workflows: a [Calliope context] appendix with workflow_id= "
        "names the Calliope graph. It is NOT render permission and there is "
        "no MCP run_workflow. Only call Calliope run_workflow when the user's "
        "own words ask to generate (or they confirm an offer). Then use that "
        "id plus prompt / aspect / attachments — do not list_workflows guess. "
        "One tagged workflow per message (first id wins). Video file "
        "attachments are context only (the worker uploads image + audio refs)."
    )


def hardening_text() -> str | None:
    """The operator-defined hardening rules, or None when unset/blank.

    Single source for the prompt section (main loop) and the sub-agent system
    prompt (swarm path), so both obey the same user-editable rules.
    """
    text = (settings.agent_hardening_prompt or "").strip()
    return text or None


async def _hardening_section(ctx: ToolContext) -> str | None:
    return hardening_text()



async def _build_scene_section(ctx: ToolContext) -> str | None:
    """Build Scene policy — single 3D-editor surface, no camera-beat lane."""
    if getattr(ctx, "origin", "chat") != "scene":
        return None
    return (
        "BUILD SCENE POLICY (shot-composer-blockout — CoS):\n"
        "1. TOOL ALLOWLIST: only shot_* tools, ask_user, list_skills/read_skill "
        "(shot-composer-blockout), and memory tools. Ban ComfyUI / workflow "
        "tool calls in this mode (run_workflow, list_workflows, enqueue_*, "
        "comfy_*) — they are mechanically denied for Build Scene sessions; "
        "if the user wants image/video generation, tell them to open "
        "Playground / AI Canvas and tag a workflow there.\n"
        "2. BRIEF GATE: before any mutate (add_object, set_transform, "
        "keyframes, poses), ask_user Lock brief → on affirmative "
        "record_build_scene_gate(gate='brief'). Soft 'maybe' = hard stop.\n"
        "3. CUT GATE: before export / shipping to pickers, ask_user Approve "
        "cut → record_build_scene_gate(gate='cut'). Soft maybe = hard stop.\n"
        "4. MOTION: stage→add_keyframe recipes only (read_skill "
        "shot-composer-blockout motion-recipes). There are NO camera-beat "
        "tools — do not invent set_camera_beats / apply_clip_v4_beats / "
        "apply_push_in_beats. Camera framing is the user's job in the "
        "TimelineStrip; do not try to drive the camera.\n"
        "5. PREVIEW: Build Scene = the Three.js viewport. It is the ONLY "
        "preview and the ONLY export/capture source — no separate render "
        "preview or export path.\n"
        "6. Never read/follow scene-to-video for this surface — that skill "
        "targets Comfy enqueue_video_jobs (closed lane here).\n"
        "7. NEVER invent or call export_video. There is no such tool. After "
        "Cut approval (ask_user → record_build_scene_gate(gate='cut')), tell "
        "the user to click **Export video** in Build Scene UI. Stills only: "
        "request_capture.\n"
        "8. Characters are blockout proxies (not limb walk cycles). "
        "Walk-across = sparse root keyframes; do not claim full locomotion "
        "until motion recipes land."
    )



def register_builtin_sections(service: SystemPromptService) -> None:
    service.register("persona", 10, _persona_section)
    service.register("mode", 20, _mode_section)
    service.register("build_scene", 25, _build_scene_section)
    service.register("workspace", 30, _workspace_digest_section)
    # Memory recall (order 35): usage-ranked preferences from harness.plugins.memory.
    # Imported lazily so composing prompts alone never composes the registry.
    from calliope.agent.harness.plugins.memory import _memory_section

    service.register("memory", 35, _memory_section)
    # Skill discovery (order 37): names + descriptions only; bodies load via
    # the read_skill tool when relevant.
    from calliope.agent.harness.plugins.skills import _skills_section

    service.register("skills", 37, _skills_section)
    service.register("mentions", 36, _mentions_section)
    service.register("discipline", 40, _tool_discipline_section)
    service.register("hardening", 50, _hardening_section)
