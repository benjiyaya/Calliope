"""Planner + sub-agent swarm orchestration.

The planner decomposes the user's goal into sub-tasks; each sub-task runs its
own scoped loop (tool subset from ROLE_TOOLS) with its own message trail
(tagged agent_name); the planner synthesizes a final answer. Simple requests
skip the swarm and use the plain single loop.

Event-log aware: the goal + workspace summary derive from the session log;
planner/sub-agent messages append ASSISTANT_MESSAGE events.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from calliope.agent.harness import get_registry
from calliope.agent.harness import log as session_log
from calliope.agent.harness.loop import (
    FINAL_STEP_NUDGE,
    MessageSink,
    RepeatGuard,
    _default_max_iterations,
    apply_fail_streak,
    run_turn,
)
from calliope.agent.harness.prompts import hardening_text
from calliope.agent.harness.registry import ToolContext
from calliope.agent.llm import LLMClient
from calliope.config import settings
from calliope.events.bus import event_bus

logger = logging.getLogger("calliope.harness.orchestrator")


def _llm_for_role(role: str) -> LLMClient:
    # Tests patch orch.LLMClient with zero-arg fakes that lack for_role;
    # prefer role resolution, fall back to a bare instance.
    factory = LLMClient
    for_role = getattr(factory, "for_role", None)
    if callable(for_role):
        return for_role(role)
    return factory()

# LLM context window bound: the last N user turns are replayed into each
# request. Tool exchanges never span user turns, so tool_call/result pairs
# always survive the trim intact.
MAX_HISTORY_USER_TURNS = 40

# _run_sub_agent returns this prefix when it called ask_user — orchestrate
# pauses the swarm on it (skips remaining tasks, no synthesis LLM call).
_PAUSED_PREFIX = "Paused: "

# Trivial-goal fast-path: one-line imperative messages skip the planner
# round-trip entirely. The single loop has every tool, so a misfire just
# loses the task-list UI — routing decision, not a permission change.
_TRIVIAL_MAX_CHARS = 160
_TRIVIAL_VERBS = (
    "rename",
    "update",
    "set",
    "change",
    "edit",
    "fix",
    "show",
    "list",
    "what",
    "how many",
    "status",
    "queue",
    "where",
    "who",
    "delete",
    "remove",
)


def _is_trivial_goal(goal: str) -> bool:
    text = (goal or "").strip().lower()
    if not text or len(text) > _TRIVIAL_MAX_CHARS:
        return False
    if any(sep in text for sep in ("\n", ";", " and then ", " then ")):
        return False
    return text.startswith(_TRIVIAL_VERBS)


def _history_char_budget() -> int | None:
    """Character budget for derived LLM history; 0 disables."""
    try:
        raw = int(getattr(settings, "agent_history_char_budget", 0) or 0)
    except (TypeError, ValueError):
        return None
    return raw if raw > 0 else None


def _last_asked_question(session_id: int) -> str:
    """The most recent unanswered ask_user question text, for the pause note."""
    try:
        for e in reversed(session_log.read_events(session_id)):
            if e.type != session_log.QUESTION_ASKED:
                continue
            return str((e.data or {}).get("question") or "question asked")
    except Exception:  # noqa: BLE001 — the note must never crash the pause path
        pass
    return "question asked"


# Tool subsets per sub-agent role. Scoped tighter than the full registry so a
# sub-agent cannot wander into another role's tools.
#
# `ask_user` is in EVERY role: a stuck sub-agent must be able to escalate to
# the user instead of grinding its step budget (the same hardening run_turn
# got for canvas/70). A question pauses the whole swarm — see _run_sub_agent.
ROLE_TOOLS: dict[str, list[str]] = {
    "story": [
        "get_workspace",
        "get_story",
        "generate_story",
        "add_beat",
        "update_beat",
        "delete_beat",
        "list_workflows",
        "ask_user",
    ],
    "script": [
        "get_workspace",
        "list_scenes",
        "list_clips",
        "generate_script",
        "break_into_shots",
        "add_clip",
        "update_clip",
        "delete_clip",
        "update_scene",
        "add_scene",
        "delete_scene",
        "reorder_scenes",
        "ask_user",
    ],
    "assets": [
        "get_workspace",
        "add_character",
        "update_character",
        "delete_character",
        "add_location",
        "update_location",
        "delete_location",
        "add_item",
        "update_item",
        "delete_item",
        "list_workflows",
        "comfy_server_info",
        "run_workflow",
        "attach_asset",
        "list_projects",
        "enqueue_asset_jobs",
        "list_jobs",
        "get_job_status",
        "wait_for_jobs",
        "post_artifact_to_canvas",
        "summarize_canvas",
        "ask_user",
    ],
    "video": [
        "get_workspace",
        "list_scenes",
        "list_clips",
        "list_workflows",
        "run_workflow",
        "enqueue_video_jobs",
        "attach_asset",
        "list_jobs",
        "get_job_status",
        "wait_for_jobs",
        "comfy_server_info",
        "ask_user",
    ],
}

PLANNER_SYSTEM = """You are the planner of an AI production swarm. Given the user's goal and the current project state, decide:

1. Whether this is a SIMPLE request (one question, one small edit, or a chat reply) — reply {"mode": "single"}.
2. Or a COMPLEX build/modify task that benefits from sub-agents — reply with a task list.

Respond with ONLY a JSON object:
{
  "mode": "single" | "swarm",
  "tasks": [
    {"role": "story|script|assets|video", "goal": "what this sub-agent must accomplish"}
  ],
  "note": "one line for the user about the plan"
}

Rules:
- The standard EDIT pipeline (story → script → add/update assets text) is swarm work: one task per role, in that order.
- Image/video GENERATION is human-in-the-loop, but the user's EXPLICIT choices grant permission: tagging a workflow (@mention), asking to "generate/render/create an image", or confirming an offer all count. When the user tagged a workflow AND named entities (characters/locations/scenes), schedule a single assets task whose goal says: run_workflow with the tagged workflow_id + per-entity prompts (character_ids=[…] for multiple characters), wait_for_jobs, then post_artifact_to_canvas for each output.
- For text-only edits (add/update characters, locations, items, scenes, story, script) with NO generation ask, schedule the edit task and DO NOT schedule render tasks.
- Film clips: video sub-agent must enqueue_video_jobs with orders (#N on Video) or scene_ids from list_scenes — ONLY the clips the user named. Never dump every scene_id. Never add_scene to attach a generated mp4. Orphan jobs (scene_id null) do not show on the Video timeline.
- A tagged workflow ([Calliope context] with workflow_id=) with entities named is a generation request: one assets task covers the text updates AND the render (the assets role has run_workflow/enqueue_asset_jobs). Never end a turn saying you lack enqueue access — the assets sub-agent has it.
- Tasks run in the order you list them. Later tasks can use earlier results.
- Keep task goals concrete and self-contained; each sub-agent sees the project state fresh.
- 2-4 tasks typical. Never more than 6.
"""


def _scoped_payload(ctx: ToolContext, allowed: list[str]) -> list[dict[str, Any]]:
    registry = get_registry()
    out: list[dict[str, Any]] = []
    for n in allowed:
        t = registry.get(n)
        if t is None or not registry._visible(t, ctx):
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
        )
    return out


async def _plan(goal: str, workspace_summary: str) -> dict[str, Any]:
    client = _llm_for_role("planner")
    try:
        text = await client.chat(
            [
                {"role": "system", "content": PLANNER_SYSTEM},
                {
                    "role": "user",
                    "content": f"Goal: {goal}\n\nProject state:\n{workspace_summary}",
                },
            ],
            temperature=0.2,
        )
    except Exception:
        # Transient LLM failure must not kill the turn — degrade to the
        # single loop, which retries the LLM with its own error handling.
        logger.exception("Planner LLM call failed; falling back to single loop")
        return {"mode": "single", "tasks": [], "note": ""}
    finally:
        await client.close()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            parsed.setdefault("mode", "single")
            parsed.setdefault("tasks", [])
            return parsed
    except json.JSONDecodeError:
        pass
    return {"mode": "single", "tasks": [], "note": ""}


async def orchestrate(
    ctx: ToolContext,
    history: list[dict[str, Any]],
    *,
    session_id: int,
    on_message: MessageSink | None = None,
) -> str:
    """Decide single vs swarm and execute. Returns the final answer text.

    History now derives from the event log (the `history` argument is kept for
    compatibility and is mutated like before for direct callers).
    """

    async def emit(message: dict[str, Any]) -> None:
        if on_message:
            await on_message({"session_id": session_id, **message})
        else:
            await event_bus.publish("agent.message", {"session_id": session_id, **message})

    session_log.backfill_from_messages(session_id)
    events = session_log.read_events(session_id)
    # Bound the LLM context to the last N user turns: long-lived sessions
    # would otherwise replay their entire history into every request.
    derived = session_log.derive_llm_history(
        events,
        max_user_turns=MAX_HISTORY_USER_TURNS,
        max_chars=_history_char_budget(),
    )
    goal = next(
        (m["content"] for m in reversed(derived) if m.get("role") == "user"),
        "",
    )

    if ctx.project_id is None:
        # Blind/sandbox session (Build Scene, sandbox canvas): every swarm
        # role's tool subset is project-scoped, so the planner can only
        # misroute here. Skip it entirely — straight to the single loop —
        # which also drops the misleading "scheduling a sub-agent" note.
        # (Checked BEFORE the workspace read: blind sessions were paying a
        # get_workspace query + summary build only to discard both.)
        history.clear()
        history.extend(derived)
        return await run_turn(ctx, history, on_message=on_message)

    # Trivial-goal fast-path: a one-line imperative ("rename character 3 to
    # Kira", "what's the queue status") does not need a planner round-trip.
    # The single loop has every tool, so a misrouted complex request still
    # completes — it just loses the task-list UI. Routing, not permission:
    # render guards are untouched either way.
    if _is_trivial_goal(goal):
        history.clear()
        history.extend(derived)
        return await run_turn(ctx, history, on_message=on_message)

    ws_result = await get_registry().execute(ctx, "get_workspace", {})
    summary = json.dumps(ws_result, ensure_ascii=False, default=str)
    if len(summary) > 3000:
        summary = summary[:3000] + "…[truncated]"

    plan = await _plan(goal, summary)
    note = (plan.get("note") or "").strip()
    if note:
        session_log.append_event(
            session_id,
            session_log.ASSISTANT_MESSAGE,
            {"content": note, "agent_name": "planner"},
        )
        await emit({"role": "assistant", "agent_name": "planner", "content": note})

    if plan.get("mode") != "swarm" or not plan.get("tasks"):
        # Single loop path (history already includes the user message).
        history.clear()
        history.extend(derived)
        return await run_turn(ctx, history, on_message=on_message)

    # ── Swarm path ──────────────────────────────────────────────
    norm_tasks: list[dict[str, str]] = []
    for t in plan["tasks"]:
        norm_tasks.append(
            {
                "role": t.get("role") or "script",
                "goal": (t.get("goal") or goal).strip(),
            }
        )
    # Persist the plan so the UI can render a live to-do table and re-derive
    # it after a reload; broadcast the full list with initial pending status.
    session_log.append_event(
        session_id,
        session_log.PLAN_CREATED,
        {"tasks": norm_tasks, "note": note},
    )
    await event_bus.publish(
        "agent.plan",
        {
            "session_id": session_id,
            "tasks": [
                {"role": t["role"], "goal": t["goal"], "status": "pending"}
                for t in norm_tasks
            ],
            "note": note,
        },
    )
    results: list[str] = []
    paused = False
    # Swarm-level steering watermark: corrections that land between tasks are
    # folded into the next task's goal context (sub-agents see the user's
    # course correction, not just the planner's original goal).
    steer_watermark = session_log.steering_max_seq(session_id)
    for i, task in enumerate(norm_tasks):
        role = task["role"]
        goal_i = task["goal"]
        if not paused:
            steer_lines = [
                str(s.data.get("content") or "").strip()
                for s in session_log.drain_steering(session_id, steer_watermark)
                if str(s.data.get("content") or "").strip()
            ]
            steer_watermark = session_log.steering_max_seq(session_id)
            if steer_lines:
                goal_i = goal_i + "\nMid-run user steering: " + " / ".join(steer_lines)
        if paused:
            # A previous sub-agent asked the user a question — later tasks may
            # depend on the answer, so running them would waste work (and
            # render permission). Skip cleanly; the user's reply re-routes.
            session_log.append_event(
                session_id,
                session_log.TASK_END,
                {"index": i, "status": "skipped"},
            )
            await event_bus.publish(
                "agent.task", {"session_id": session_id, "index": i, "status": "skipped"}
            )
            session_log.append_event(
                session_id,
                session_log.ASSISTANT_MESSAGE,
                {
                    "content": f"Skipped (waiting for the user's answer): {goal_i}",
                    "agent_name": f"{role}-agent",
                },
            )
            await emit(
                {
                    "role": "assistant",
                    "agent_name": f"{role}-agent",
                    "content": f"Skipped (waiting for the user's answer): {goal_i}",
                }
            )
            continue
        allowed = ROLE_TOOLS.get(role, ROLE_TOOLS["script"])
        session_log.append_event(session_id, session_log.TASK_START, {"index": i})
        await event_bus.publish(
            "agent.task",
            {"session_id": session_id, "index": i, "role": role, "goal": goal_i, "status": "running"},
        )
        session_log.append_event(
            session_id,
            session_log.ASSISTANT_MESSAGE,
            {"content": f"Starting: {goal_i}", "agent_name": f"{role}-agent"},
        )
        await emit(
            {
                "role": "assistant",
                "agent_name": f"{role}-agent",
                "content": f"Starting: {goal_i}",
            }
        )
        sub_history: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    f"You are the {role} sub-agent. Goal: {goal_i}\n"
                    f"Project: #{ctx.project_id}. Complete your goal with your "
                    "available tools, then reply with a concise summary of what "
                    "you did and what the next sub-agent should know."
                ),
            }
        ]
        try:
            answer = await _run_sub_agent(
                ctx, sub_history, allowed, agent_name=f"{role}-agent", on_message=on_message
            )
            results.append(f"[{role}] {answer}")
            session_log.append_event(
                session_id,
                session_log.TASK_END,
                {"index": i, "status": "done"},
            )
            await event_bus.publish(
                "agent.task", {"session_id": session_id, "index": i, "status": "done"}
            )
            session_log.append_event(
                session_id,
                session_log.ASSISTANT_MESSAGE,
                {"content": answer, "agent_name": f"{role}-agent"},
            )
            await emit(
                {
                    "role": "assistant",
                    "agent_name": f"{role}-agent",
                    "content": answer,
                }
            )
            if answer.startswith(_PAUSED_PREFIX):
                # The sub-agent called ask_user: the question card is already
                # persisted (question/asked event), so stop the swarm here
                # instead of running tasks whose inputs are unknown.
                paused = True
        except Exception as exc:  # noqa: BLE001
            # Some exceptions stringify EMPTY (httpx.ReadTimeout/ReadError,
            # TimeoutError) — always name the type, and keep the traceback
            # (observed live 2026-08-25: "Sub-agent failed: " with nothing
            # after the colon, because a deploy restart killed the in-flight
            # stream and the ReadError carried no message).
            logger.exception("Sub-agent %s failed", role)
            detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            results.append(f"[{role}] FAILED: {detail}")
            session_log.append_event(
                session_id,
                session_log.TASK_END,
                {"index": i, "status": "failed"},
            )
            await event_bus.publish(
                "agent.task", {"session_id": session_id, "index": i, "status": "failed"}
            )
            session_log.append_event(
                session_id,
                session_log.ASSISTANT_MESSAGE,
                {
                    "content": f"Sub-agent failed: {detail}",
                    "agent_name": f"{role}-agent",
                    "status": "error",
                },
            )
            await emit(
                {
                    "role": "assistant",
                    "agent_name": f"{role}-agent",
                    "content": f"Sub-agent failed: {detail}",
                    "status": "error",
                }
            )

    if paused:
        # Deterministic pause message — no synthesis LLM call. The question
        # card is in the log; the user's next message resumes the work.
        final = (
            "Paused for your answer: " + _last_asked_question(session_id)
            + ". Remaining tasks were not started — reply to continue."
        )
        session_log.append_event(
            session_id,
            session_log.ASSISTANT_MESSAGE,
            {"content": final, "agent_name": None},
        )
        await emit({"role": "assistant", "content": final})
        return final

    # Synthesis: plain LLM call over sub-agent reports.
    synthesis_in = (
        "User goal:\n"
        f"{goal}\n\nSub-agent reports:\n" + "\n\n".join(results)
        + "\n\nWrite a concise final summary for the user: what was done, "
        "job ids enqueued, and any failures. Plain text."
    )
    client = _llm_for_role("planner")
    try:
        final = await client.chat(
            [
                {
                    "role": "system",
                    "content": "You are the swarm's lead. Summarize sub-agent work for the user. Be concrete.",
                },
                {"role": "user", "content": synthesis_in},
            ],
            temperature=0.3,
        )
    except Exception:
        # All sub-agent work is already persisted in the event log — losing
        # the whole answer over a synthesis failure is unacceptable. Fall
        # back to the deterministic template from the reports themselves.
        logger.exception("Swarm synthesis LLM call failed; using template summary")
        final = "Work finished. Sub-agent reports:\n\n" + "\n\n".join(results)
    finally:
        await client.close()
    session_log.append_event(
        session_id,
        session_log.ASSISTANT_MESSAGE,
        {"content": final, "agent_name": None},
    )
    await emit({"role": "assistant", "content": final})
    return final


async def _run_sub_agent(
    ctx: ToolContext,
    sub_history: list[dict[str, Any]],
    allowed_tools: list[str],
    *,
    agent_name: str,
    max_iterations: int | None = None,
    on_message: MessageSink | None = None,
) -> str:
    """A scoped agentic loop for one sub-agent (non-streaming variant).

    Unlike run_turn this does not publish token events for every sub-agent —
    only tool events carry the agent_name so the UI can group them.
    """
    if max_iterations is None:
        max_iterations = _default_max_iterations()
    registry = get_registry()

    async def emit(message: dict[str, Any]) -> None:
        if on_message:
            await on_message({"session_id": ctx.session_id, **message})
        else:
            await event_bus.publish("agent.message", {"session_id": ctx.session_id, **message})

    role = agent_name.removesuffix("-agent")
    client = _llm_for_role(role)
    messages = list(sub_history)
    system = (
        "You are a specialized sub-agent in Calliope's production swarm. "
        "Complete your goal with the tools available. Reply with a concise "
        "summary when done — no tool call.\n"
        "Image/video generation (enqueue_asset_jobs / enqueue_video_jobs / "
        "run_workflow) is human-in-the-loop: only call it when the user "
        "explicitly asked to generate. A workflow_id= appendix is not "
        "permission. There is no MCP run_workflow. Film clips: "
        "enqueue_video_jobs with orders (Video #N) or scene_ids from "
        "list_scenes — only the clips they named, never the whole timeline. "
        "Never add_scene to attach an mp4. For text-only edits, "
        "do the edit and stop."
    )
    hardening = hardening_text()
    if hardening:
        system += "\n\n" + hardening
    # Same safety nets as run_turn — the swarm must not be a second-class
    # citizen (a stuck sub-agent ground its whole budget with no repeat
    # guard and no change-course directive before this).
    repeat = RepeatGuard()
    fail_streak = 0
    # Steering watermark, same contract as run_turn: drain at step
    # boundaries only (after the previous step's tool results).
    steer_watermark = session_log.steering_max_seq(ctx.session_id)
    final = ""
    try:
        for iteration in range(1, max_iterations + 1):
            for s in session_log.drain_steering(ctx.session_id, steer_watermark):
                steer_watermark = max(steer_watermark, s.seq)
                messages.append(
                    {"role": "user", "content": session_log.steering_user_content(s.data)}
                )
            payload = _scoped_payload(ctx, allowed_tools)
            step_messages = list(messages)
            if max_iterations - iteration == 0:
                # Final-step budget nudge: wrap up, don't start new work.
                step_messages.append({"role": "user", "content": FINAL_STEP_NUDGE})
            msg = await client.chat_with_tools(
                [{"role": "system", "content": system}] + step_messages,
                temperature=0.3,
                tools=payload or None,
            )
            tool_calls = msg.get("tool_calls") or []
            messages.append(msg)
            if not tool_calls:
                final = (msg.get("content") or "").strip()
                break
            for tc in tool_calls:
                # Some OpenAI-compatible servers omit id/name on tool calls —
                # fall back like loop.py's streaming accumulator does.
                fn = tc.get("function") or {}
                name = fn.get("name") or "unknown_tool"
                call_id = tc.get("id") or f"call_{id(tc) & 0xFFFFFF:x}"
                raw = fn.get("arguments") or "{}"
                try:
                    parsed = json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError:
                    parsed = None
                session_log.append_event(
                    ctx.session_id,
                    session_log.TOOL_CALL,
                    {
                        "call_id": call_id,
                        "tool_name": name,
                        "arguments": raw,
                        "agent_name": agent_name,
                    },
                )
                await event_bus.publish(
                    "agent.tool",
                    {
                        "session_id": ctx.session_id,
                        "agent_name": agent_name,
                        "phase": "start",
                        "tool": name,
                        "args": parsed if isinstance(parsed, dict) else None,
                    },
                )
                if parsed is None:
                    result = {"ok": False, "error": f"Invalid JSON arguments: {raw[:200]}"}
                elif not isinstance(parsed, dict):
                    # Valid JSON but not an object — reject with a message the
                    # model can self-correct from (tools call args.get).
                    result = {
                        "ok": False,
                        "error": (
                            f"Tool arguments must be a JSON object, got "
                            f"{type(parsed).__name__}: {raw[:200]}"
                        ),
                    }
                elif name not in allowed_tools:
                    # Enforce the role allowlist at execute time too — payload
                    # scoping only filters what the model SEES; a hallucinated
                    # out-of-role call must not run.
                    result = {"ok": False, "error": f"Tool not available to this role: {name}"}
                else:
                    intercepted = repeat.intercept(name, parsed)
                    if intercepted is not None:
                        result = intercepted
                    else:
                        result = await registry.execute(ctx, name, parsed)
                        repeat.record(name, parsed, result)
                session_log.append_event(
                    ctx.session_id,
                    session_log.TOOL_RESULT,
                    {"call_id": call_id, "tool_name": name, "result": result, "agent_name": agent_name},
                )
                await event_bus.publish(
                    "agent.tool",
                    {
                        "session_id": ctx.session_id,
                        "agent_name": agent_name,
                        "phase": "finish",
                        "tool": name,
                        "result": result,
                    },
                )
                result_text = json.dumps(result, ensure_ascii=False, default=str)
                if len(result_text) > session_log.TOOL_RESULT_TRUNCATE:
                    result_text = result_text[: session_log.TOOL_RESULT_TRUNCATE] + session_log.TRUNCATE_NOTE
                # Thrash killer — same net as run_turn.
                if isinstance(result, dict) and result.get("ok") is False:
                    fail_streak += 1
                elif isinstance(result, dict):
                    fail_streak = 0
                result_text += apply_fail_streak(result, fail_streak)
                messages.append(
                    {"role": "tool", "tool_call_id": call_id, "content": result_text}
                )
                await emit(
                    {
                        "role": "tool",
                        "agent_name": agent_name,
                        "tool_name": name,
                        "tool_args": parsed if isinstance(parsed, dict) else None,
                        "tool_result": result,
                        "content": "",
                    }
                )
                # ask_user pauses the whole swarm: the question card waits for
                # the user's answer; later tasks may depend on it. The loop
                # must not burn its remaining steps polling.
                if isinstance(result, dict) and result.get("awaiting_user_input"):
                    return _PAUSED_PREFIX + (result.get("question") or "question asked")
        else:
            final = (
                "Reached step budget. Here is where things stand — the goal "
                "may be partially complete."
            )
    finally:
        await client.close()
    return final or "Done."
