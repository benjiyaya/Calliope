"""Tool registry with a guarded execution pipeline.

Ported from the deepseek-harness `ctx.tools` seam: a tool is a registration
(schema + async executor) contributed by a plugin; execution runs through a
`pre-execute → execute → post-execute` waterfall so policy (destructive-action
guarding, approval, audit) lives outside the loop and outside tool bodies.

Key invariants kept from the previous registry:
- `project_id` is injected via ToolContext — never an LLM argument.
- `requires_project` / `blind_only` enforce sandbox scoping at BOTH payload
  assembly and execution time.
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from calliope.config import settings
from calliope.db import get_db, row_to_dict
from calliope.events.bus import event_bus

# ─────────────────────────────────────────────────────────────────────────
# Scene tool scope (Build Scene surface)
# ─────────────────────────────────────────────────────────────────────────

# Build Scene sessions (origin='scene') are the 3D blockout surface: their
# toolset is the shot_* set plus this minimal base. Enforced at BOTH payload
# assembly and execute time (same two-layer pattern as requires_project).
# Defined here (not harness/__init__) to avoid a circular import; re-exported
# there for tests/UI consumers.
GUARD_SCENE_TOOL_SCOPE = "guard_scene_tool_scope"
_SCENE_ALLOWED_CATEGORIES = {"shot"}
_SCENE_ALLOWED_TOOLS = {
    "ask_user",
    "list_skills",
    "read_skill",
    "save_memory",
    "list_memories",
    "forget_memory",
}

# Invented agent tool — there is no export_video; Cut + UI Export video instead.
GUARD_NO_EXPORT_VIDEO_TOOL = "guard_no_export_video_tool"
EXPORT_VIDEO_DENIED_NAMES = frozenset({"export_video", "exportVideo"})


def _tool_timeout_sec() -> float:
    """Per-tool wall-clock cap from Settings; 0 = disabled."""
    try:
        t = float(settings.agent_tool_timeout_sec)
    except (TypeError, ValueError, AttributeError):
        return 0.0
    return max(0.0, t)


def _scene_scoped(t: "ToolDefinition") -> bool:
    return getattr(t, "category", None) in _SCENE_ALLOWED_CATEGORIES or t.name in _SCENE_ALLOWED_TOOLS


# ─────────────────────────────────────────────────────────────────────────
# Context & definitions
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class ToolContext:
    """Per-run context. `project_id` is the session's workspace binding:
    None means a blind/sandbox session (only project-creating tools allowed).
    `origin` stamps WHERE the session was born ('chat' | 'scene') — scene
    sessions are Build Scene's blockout surface and get the shot-tool-only
    payload; it says nothing about project linking."""

    session_id: int
    project_id: int | None = None
    origin: str = "chat"


@dataclass
class ToolDefinition:
    """One registered tool: model-facing schema + async executor."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema (object)
    executor: Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any] | list | str]]
    requires_project: bool = True  # False → also available in blind sessions
    blind_only: bool = False  # True → hidden once the session is linked
    category: str = "general"
    destructive: bool = False  # flagged in descriptions; pre-execute guard uses it
    requires_approval: bool = False  # HITL: blocked unless the user explicitly asked
    # True → exempt from the per-tool wall-clock timeout (agent_tool_timeout_sec);
    # the tool owns its own wait contract (e.g. wait_for_jobs → queue_poll_timeout_sec).
    long_running: bool = False


# Hook results — the vocabulary of the execution pipeline.
# pre-execute:  allow | deny  (veto/replace before the body runs)
# post-execute: keep | replace(result)  (audit/redaction)


@dataclass
class PreExecuteDecision:
    kind: str  # "allow" | "deny"
    error: str | None = None
    reason_code: str | None = None  # stable machine token for UI/tests, e.g. guard_render_approval


ALLOW = PreExecuteDecision(kind="allow")


def allow() -> PreExecuteDecision:
    return ALLOW


def deny(reason: str, code: str | None = None) -> PreExecuteDecision:
    return PreExecuteDecision(kind="deny", error=reason, reason_code=code)


@dataclass
class PostExecuteDecision:
    kind: str  # "keep" | "replace"
    result: dict[str, Any] | None = None


KEEP = PostExecuteDecision(kind="keep")


def replace_result(result: dict[str, Any]) -> PostExecuteDecision:
    return PostExecuteDecision(kind="replace", result=result)


# Hook signatures (all optional pipeline stages)
PreExecuteHook = Callable[[ToolContext, "ToolDefinition", dict[str, Any]], Awaitable[PreExecuteDecision]]
PostExecuteHook = Callable[
    [ToolContext, "ToolDefinition", dict[str, Any], dict[str, Any]], Awaitable[PostExecuteDecision]
]


@dataclass
class ToolRegistry:
    """Scoped tool registry. Plugins contribute definitions; the loop and the
    sub-agent runner request the payload for a ToolContext."""

    tools: dict[str, ToolDefinition] = field(default_factory=dict)
    pre_execute: list[PreExecuteHook] = field(default_factory=list)
    post_execute: list[PostExecuteHook] = field(default_factory=list)

    async def _execute_with_timeout(
        self, t: "ToolDefinition", ctx: ToolContext, args: dict[str, Any]
    ) -> dict[str, Any]:
        timeout = _tool_timeout_sec()
        if timeout <= 0 or t.long_running:
            return await t.executor(ctx, args)
        return await asyncio.wait_for(t.executor(ctx, args), timeout=timeout)

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self.tools:
            raise ValueError(f"Tool already registered: {definition.name}")
        self.tools[definition.name] = definition

    def on_pre_execute(self, hook: PreExecuteHook) -> None:
        self.pre_execute.append(hook)

    def on_post_execute(self, hook: PostExecuteHook) -> None:
        self.post_execute.append(hook)

    # ── scoping ──────────────────────────────────────────────────

    def _visible(self, t: ToolDefinition, ctx: ToolContext) -> bool:
        if t.requires_project and ctx.project_id is None:
            return False
        if t.blind_only and ctx.project_id is not None:
            return False
        # Build Scene surface: shot tools + minimal base ONLY. A missing tool
        # is harder to misuse than one that fails at execute time.
        if ctx.origin == "scene" and not _scene_scoped(t):
            return False
        # Hide render tools until the user asks — the model still "knows"
        # run_workflow from earlier turns, but a missing tool is harder to
        # call than one that only fails in pre-execute (HITL cards).
        # Approval via a scoped question card counts the same as prose.
        if t.requires_approval:
            from calliope.agent.harness.policy import user_allows_render

            if not user_allows_render(ctx):
                return False
        return True

    def openai_payload(self, ctx: ToolContext) -> list[dict[str, Any]]:
        """OpenAI tool-calling payload for the tools available in this context."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self.tools.values()
            if self._visible(t, ctx)
        ]

    def get(self, name: str) -> ToolDefinition | None:
        return self.tools.get(name)

    # ── execution pipeline ───────────────────────────────────────

    async def execute(self, ctx: ToolContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run one tool through the guarded pipeline. Returns a JSON-serializable dict."""
        t = self.tools.get(name)
        if t is None:
            if name in EXPORT_VIDEO_DENIED_NAMES:
                return {
                    "ok": False,
                    "reason_code": GUARD_NO_EXPORT_VIDEO_TOOL,
                    "error": (
                        "There is no tool named export_video. Build Scene "
                        "export is human-gated: ask_user Cut approval → "
                        "record_build_scene_gate(gate='cut') → tell the user to "
                        "click **Export video** in the Build Scene UI. "
                        "For stills only use request_capture. Do not invent "
                        "export_video; do not call enqueue_video_jobs or follow "
                        "scene-to-video — that lane is Comfy closed for origin=scene. "
                        "Prefer read_skill(\"shot-composer-blockout\")."
                    ),
                }
            return {"ok": False, "error": f"Unknown tool: {name}"}
        if t.requires_project and ctx.project_id is None:
            return {
                "ok": False,
                "error": "This tool requires a linked project. Create one with create_project first.",
            }
        if t.blind_only and ctx.project_id is not None:
            return {
                "ok": False,
                "error": (
                    "This tool is only available in a sandbox (unlinked) session. "
                    "Call unlink_project first to return this session to sandbox."
                ),
            }
        if ctx.origin == "scene" and not _scene_scoped(t):
            extra = ""
            if name in {"enqueue_video_jobs", "enqueue_asset_jobs", "run_workflow"} or name.startswith(
                ("enqueue_", "comfy_")
            ):
                extra = (
                    " scene-to-video / enqueue_video_jobs is the WRONG skill/lane "
                    "for Build Scene — use read_skill(\"shot-composer-blockout\") "
                    "and Cut → user clicks Export video."
                )
            return {
                "ok": False,
                "reason_code": GUARD_SCENE_TOOL_SCOPE,
                "error": (
                    "This is a Build Scene session: "
                    "only shot_* tools plus ask_user / skills / memory are available. "
                    "ComfyUI and workflow tools (run_workflow, list_workflows, "
                    "enqueue_*, comfy_*) are banned here. Build with shot_* "
                    "recipes, then Capture / Export video (Cut-gated) — "
                    "pick the clip from Playground later; do not generate via Comfy "
                    "from this chat."
                    + extra
                ),
            }

        decision = ALLOW
        for hook in self.pre_execute:
            outcome = hook(ctx, t, args)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            decision = outcome
            if decision.kind == "deny":
                denied: dict[str, Any] = {"ok": False, "error": f"blocked: {decision.error}"}
                if decision.reason_code:
                    denied["reason_code"] = decision.reason_code
                return denied
        try:
            result = await self._execute_with_timeout(t, ctx, args)
        except TimeoutError:
            # Per-tool wall-clock cap (0 = disabled): a hung tool must stall
            # its step, not the session. CancelledError passes through so the
            # Stop button keeps working.
            return {
                "ok": False,
                "error": f"{name} timed out after {_tool_timeout_sec():.0f}s",
                "timeout": True,
            }
        except Exception as exc:  # noqa: BLE001 — tool errors are loop-feedback, not crashes
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if isinstance(result, dict):
            result.setdefault("ok", True)
        else:
            result = {"ok": True, "result": result}
        for hook in self.post_execute:
            d = hook(ctx, t, args, result)
            if inspect.isawaitable(d):
                d = await d
            if d.kind == "replace":
                result = d.result or {}
        return result


# ─────────────────────────────────────────────────────────────────────────
# Session-changed notification (kept from the old registry)
# ─────────────────────────────────────────────────────────────────────────


async def publish_session_updated(session_id: int) -> None:
    """Fire agent.session.updated with the enriched session (project info
    included) so the UI can flip sandbox→linked live."""
    from calliope.routers.agent import session_event_payload

    await event_bus.publish(
        "agent.session.updated",
        {"session": await session_event_payload(session_id)},
    )


# ─────────────────────────────────────────────────────────────────────────
# Shared DB helpers (used by tool executors)
# ─────────────────────────────────────────────────────────────────────────


def _db():
    return get_db(settings.db_path)


def _project_or_error(conn, project_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return row_to_dict(row) if row else None


def _project_stats(project_id: int, conn) -> dict[str, int]:
    cur = conn.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM scenes WHERE project_id = :pid) AS scene_count,
            (SELECT COUNT(*) FROM characters WHERE project_id = :pid) AS character_count,
            (SELECT COUNT(*) FROM characters WHERE project_id = :pid AND sheet_path IS NOT NULL)
                + (SELECT COUNT(*) FROM locations WHERE project_id = :pid AND reference_image_path IS NOT NULL)
                + (SELECT COUNT(*) FROM items WHERE project_id = :pid AND reference_image_path IS NOT NULL) AS asset_ready_count,
            (SELECT COUNT(*) FROM characters WHERE project_id = :pid)
                + (SELECT COUNT(*) FROM locations WHERE project_id = :pid)
                + (SELECT COUNT(*) FROM items WHERE project_id = :pid) AS asset_total_count
        """,
        {"pid": project_id},
    )
    return dict(cur.fetchone())
