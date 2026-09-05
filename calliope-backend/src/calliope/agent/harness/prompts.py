"""System-prompt assembly from registered sections.

Ported from the deepseek-harness `ctx.systemPrompt` seam: instead of one
hardcoded string, plugins contribute ordered sections. `assemble(ctx)` renders
them (a section may skip itself, e.g. workspace digest in sandbox mode).
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

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
    seq: int = 0  # registration-order tiebreaker


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
                # One broken section must not kill the whole system prompt.
                logger.exception("Prompt section %r failed; skipping", section.key)
                continue
            if text:
                parts.append(text.strip())
        return "\n\n".join(p for p in parts if p)


# ─────────────────────────────────────────────────────────────────────────
# Built-in sections (port of the previous monolithic prompt)
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
        "- Film clips: enqueue_video_jobs with orders or scene_ids for ONLY "
        "the clips they named. Never dump every scene_id. Never omit the "
        "list (that is not 'all'). all_scenes=true only if they said all/"
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
        "Tool discipline:\n"
        "1. READ BEFORE WRITE. Before generate_*, update_*, add_*, delete_*, "
        "reorder_*, or enqueue_*: call get_workspace (or the matching list "
        "tool) to see current state. Never edit blind.\n"
        "2. ONE tool call at a time — wait for its result before the next. "
        "Never fabricate results or guess ids.\n"
        "3. DESTRUCTIVE TOOLS: generate_story and generate_script with "
        "replace=true DELETE existing data (beats/characters/locations, or all "
        "scenes). When the project already has content, the call is BLOCKED "
        "unless the user's latest message explicitly asks for/confirms the "
        "replacement. If it is blocked, ask the user; once they answer yes "
        "(e.g. 'yes, replace'), retry replace=true and it will go through. "
        "Otherwise pass replace=false to append.\n"
        "4. RENDERS ARE HUMAN-IN-THE-LOOP. enqueue_asset_jobs, enqueue_video_jobs, "
        "and run_workflow queue real renders. NEVER call them unless the user's "
        "latest message explicitly asks for generation (e.g. 'generate the images', "
        "'render the video', 'create an image') or confirms your offer. "
        "Text edits — add_item, add_character, add_location, update_scene, "
        "generate_story, generate_script, etc. — are NOT permission to render. "
        "After text edits, if images/videos are missing, tell the user and ASK "
        "whether to generate; wait for their yes — do not call run_workflow in "
        "the same turn as the question. Render tools are hidden until they ask. "
        "Also call comfy_server_info first; if ComfyUI is unreachable or dry_run "
        "is on, say so and stop.\n"
        "4b. NO COMFY MCP. There is no MCP run_workflow / comfy_run_workflow / "
        "comfy_run_template / comfy_search_templates. The only comfy_* tool is "
        "comfy_server_info (health). Calliope run_workflow is the HTTP queue "
        "(tagged @workflow), not MCP. Use enqueue_asset_jobs / enqueue_video_jobs "
        "for project assets and clips.\n"
        "5. ASSETS BEFORE VIDEO: reference-based video needs character sheets "
        "and location images to exist. After enqueue_asset_jobs, wait_for_jobs "
        "and only enqueue_video_jobs once images are ready.\n"
        "5b. SCENE CLIPS ARE WIRED IN CODE. On a linked film, video generate "
        "must be enqueue_video_jobs (orders=#N or scene_ids) or run_workflow "
        "with scene_id from list_scenes. The worker writes scenes.video_path "
        "from job.scene_id. Do not add_scene to 'fix' a missing clip. If "
        "wait_for_jobs returns wired_to_scene=false, call attach_asset "
        "target=scene with that job_id and the existing scene_id.\n"
        "5c. #N IS ORDER. The Video page shows #1, #2… as order_index. "
        "scene_id is a different, larger database id. list_scenes / "
        "update_scene / delete_scene / enqueue_video_jobs accept order so "
        "you can follow the user. Search with list_scenes query= or "
        "orders=[25,26]. Never enqueue more clips than they named.\n"
        "6. Standard EDIT pipeline (text only, no renders): create_project → "
        "generate_story → generate_script → add/update assets (characters, "
        "locations, items) as needed. Rendering is a SEPARATE step that only "
        "runs after the user explicitly asks for it.\n"
        "7. FINISH: when the request is complete, reply with a concise "
        "plain-text summary (what was created/changed, job ids enqueued, any "
        "failures) with NO tool call.\n"
        "8. MEMORY: save_memory records a DURABLE preference or convention "
        "for future sessions — call it the moment the user states one "
        "('I always want…', 'never do…', 'prefer terse', 'this project uses "
        "X style') or corrects you in a way that will recur. One atomic "
        "sentence. Do NOT save one-off task details or ids. Before saving, "
        "list_memories; if a memory now contradicts an older one, "
        "forget_memory the stale one first. Saved memories are injected into "
        "your system prompt each turn — follow them."
    )


async def _mentions_section(ctx: ToolContext) -> str | None:
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


def register_builtin_sections(service: SystemPromptService) -> None:
    service.register("persona", 10, _persona_section)
    service.register("mode", 20, _mode_section)
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
