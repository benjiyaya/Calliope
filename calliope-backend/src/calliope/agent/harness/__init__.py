"""Agent harness package.

Composition root: builds the shared ToolRegistry + SystemPromptService once,
loads all plugins, and installs the built-in policy hooks (destructive-action
guard). Modeled on the deepseek-harness plugin composition: the loop, the
orchestrator, and tests all program against these services, never against
individual plugins.
"""
from __future__ import annotations

import logging
import re

from calliope.agent.harness.plugins import load_all as _load_plugins
from calliope.agent.harness.prompts import (
    SystemPromptService,
    register_builtin_sections,
)
from calliope.agent.harness.registry import (
    PreExecuteDecision,
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    allow,
    deny,
)

logger = logging.getLogger("calliope.harness")


# Affirmation / destructive-intent cues used to recognise an explicit user
# "yes" (or "replace it" / "start over" request) for the destructive guard.
_CONFIRM_RE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok|okay|k|confirm(?:ed)?|proceed|"
    r"go\s+ahead|do\s+it|please\s+do|overwrite|replace|regenerate|"
    r"redo|re-?do|start\s+over|restart|delete|wipe|reset|from\s+scratch|"
    r"go\s+for\s+it|fine|sounds\s+good|that'?s\s+fine)\b",
    re.IGNORECASE,
)
_NEGATE_RE = re.compile(
    r"\b(no|not|don'?t|do\s+not|cancel|stop|never|abort|skip|hold\s+on|wait)\b",
    re.IGNORECASE,
)

# Image/video *generation* intent cues, used by the render-approval guard. We
# deliberately avoid a bare "generate" (which would also match "generate the
# story") — only unambiguous render words unlock generation.
_RENDER_REQUEST_RE = re.compile(
    r"\b(render\w*|image\w*|video\w*|portrait\w*|sheet\w*|artwork\w*|visual\w*|thumbnail\w*)\b",
    re.IGNORECASE,
)


def _is_confirmation(text: str) -> bool:
    """A terse, non-negated message carrying an explicit affirmative cue."""
    t = (text or "").strip()
    if not t or len(t) > 200:
        return False
    if _NEGATE_RE.search(t):
        return False
    return bool(_CONFIRM_RE.search(t))


def _user_confirmed_replacement(ctx: ToolContext) -> bool:
    """True when the user's latest message explicitly asks for / confirms a
    destructive replace (derived from the event log — no separate state)."""
    from calliope.agent.harness import log as session_log

    return _is_confirmation(session_log.latest_user_message(ctx.session_id) or "")


def _is_render_request(text: str) -> bool:
    """True when the message explicitly asks for image/video generation."""
    t = (text or "").strip()
    if not t or _NEGATE_RE.search(t):
        return False
    return bool(_RENDER_REQUEST_RE.search(t))


def _render_approval_guard(ctx: ToolContext, t: ToolDefinition, args: dict) -> PreExecuteDecision:
    """Human-in-the-loop guard for image/video generation.

    Rendering (enqueue_asset_jobs / enqueue_video_jobs / run_workflow)
    queues real, expensive, side-effecting work. It must never run unless
    the user's latest message explicitly asks for generation, or tersely confirms
    the agent's offer. Text edits (add_item, add_character, add_location,
    update_scene, generate_story/script, …) do NOT grant render permission —
    this is what previously let the assets-agent auto-generate images after a
    "create a Misc. Item" request.
    """
    if not t.requires_approval:
        return allow()
    from calliope.agent.harness import log as session_log

    latest = session_log.latest_user_message(ctx.session_id) or ""
    if _is_render_request(latest) or _is_confirmation(latest):
        return allow()
    return deny(
        "Image/video generation is human-in-the-loop and needs explicit user approval. "
        "The user's latest message does not ask to render. Complete the requested text "
        "edits, then ASK whether they want images/videos generated — do not enqueue "
        "until they confirm (e.g. 'yes, generate the images')."
    )


def _destructive_guard(ctx: ToolContext, t: ToolDefinition, args: dict) -> PreExecuteDecision:
    """Block silent destructive *regeneration* unless the user asked for it.

    Scope: tools with a `replace` parameter (generate_story / generate_script)
    — "replace=true (default) deletes everything first". A destructive tool
    WITHOUT a replace param (delete_scene, comfy lifecycle tools) acts on one
    explicit target the model named, so it is not blocked here.

    A non-empty project is protected unless the user's latest message is an
    explicit confirmation / regeneration request (e.g. "yes, replace it").
    That turns "ask the user first" from a dead-end into a real confirm step.
    """
    if not t.destructive:
        return allow()
    has_replace_param = "replace" in t.parameters.get("properties", {})
    if not has_replace_param:
        return allow()
    replace = bool(args.get("replace", True))
    if not replace:
        return allow()
    # Destructive path: only allow when the project is effectively empty, or
    # the user explicitly confirmed.
    from calliope.agent.harness.registry import _db

    if ctx.project_id is None:
        return allow()
    conn = _db()
    try:
        scenes = conn.execute(
            "SELECT COUNT(*) AS n FROM scenes WHERE project_id = ?", (ctx.project_id,)
        ).fetchone()["n"]
        beats = conn.execute(
            "SELECT COUNT(*) AS n FROM story_beats WHERE project_id = ?", (ctx.project_id,)
        ).fetchone()["n"]
        chars = conn.execute(
            "SELECT COUNT(*) AS n FROM characters WHERE project_id = ?", (ctx.project_id,)
        ).fetchone()["n"]
        locs = conn.execute(
            "SELECT COUNT(*) AS n FROM locations WHERE project_id = ?", (ctx.project_id,)
        ).fetchone()["n"]
        items = conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE project_id = ?", (ctx.project_id,)
        ).fetchone()["n"]
    finally:
        conn.close()
    has_content = (scenes + beats + chars + locs + items) > 0
    if not has_content:
        return allow()
    if _user_confirmed_replacement(ctx):
        return allow()
    return deny(
        f"Project #{ctx.project_id} already has content ({scenes} scenes, {beats} beats, "
        f"{chars} characters, {locs} locations, {items} items). Ask the user to confirm before replacing; "
        "once they confirm (e.g. 'yes, replace'), call this tool again. "
        "To append instead, pass replace=false."
    )


_BUILT = False
_registry: ToolRegistry | None = None
_prompts: SystemPromptService | None = None


def build_harness() -> tuple[ToolRegistry, SystemPromptService]:
    """Compose the harness (idempotent). Returns (registry, prompts)."""
    global _BUILT, _registry, _prompts
    if _BUILT:
        assert _registry is not None and _prompts is not None
        return _registry, _prompts
    registry = ToolRegistry()
    prompts = SystemPromptService()
    _load_plugins()
    from calliope.agent.harness.plugins import workspace, story, script, render  # noqa: E402

    workspace.register(registry)
    story.register(registry)
    script.register(registry)
    render.register(registry)
    registry.on_pre_execute(_destructive_guard)
    registry.on_pre_execute(_render_approval_guard)
    register_builtin_sections(prompts)
    _registry, _prompts = registry, prompts
    _BUILT = True
    logger.info("Harness composed: %d tools", len(registry.tools))
    return registry, prompts


def get_registry() -> ToolRegistry:
    registry, _ = build_harness()
    return registry


def get_prompts() -> SystemPromptService:
    _, prompts = build_harness()
    return prompts
