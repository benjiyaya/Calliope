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
from calliope.agent.harness.loop import MessageSink, run_turn, _default_max_iterations
from calliope.agent.harness.prompts import hardening_text
from calliope.agent.harness.registry import ToolContext
from calliope.agent.llm import LLMClient
from calliope.events.bus import event_bus

logger = logging.getLogger("calliope.harness.orchestrator")

# LLM context window bound: the last N user turns are replayed into each
# request. Tool exchanges never span user turns, so tool_call/result pairs
# always survive the trim intact.
MAX_HISTORY_USER_TURNS = 40

# Tool subsets per sub-agent role. Scoped tighter than the full registry so a
# sub-agent cannot wander into another role's tools.
ROLE_TOOLS: dict[str, list[str]] = {
    "story": [
        "get_workspace",
        "get_story",
        "generate_story",
        "add_beat",
        "update_beat",
        "delete_beat",
        "list_workflows",
    ],
    "script": [
        "get_workspace",
        "list_scenes",
        "generate_script",
        "update_scene",
        "add_scene",
        "delete_scene",
        "reorder_scenes",
    ],
    "assets": [
        "get_workspace",
        "update_character",
        "update_location",
        "update_item",
        "list_workflows",
        "comfy_server_info",
        "run_workflow",
        "attach_asset",
        "list_projects",
        "enqueue_asset_jobs",
        "list_jobs",
        "get_job_status",
        "wait_for_jobs",
    ],
    "video": [
        "get_workspace",
        "list_scenes",
        "list_workflows",
        "run_workflow",
        "enqueue_video_jobs",
        "attach_asset",
        "list_jobs",
        "get_job_status",
        "wait_for_jobs",
        "comfy_server_info",
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
- Image/video GENERATION is human-in-the-loop: only include assets/video RENDER tasks (enqueue_asset_jobs / enqueue_video_jobs / run_workflow) when the user explicitly asked to generate. For text-only edits (add/update characters, locations, items, scenes, story, script), schedule the edit task and DO NOT schedule render tasks.
- Film clips: video sub-agent must enqueue_video_jobs with orders (#N on Video) or scene_ids from list_scenes — ONLY the clips the user named. Never dump every scene_id. Never add_scene to attach a generated mp4. Orphan jobs (scene_id null) do not show on the Video timeline.
- A tagged workflow ([Calliope context] with workflow_id=) is a SIMPLE request — reply {"mode": "single"}. The main loop may call run_workflow only if the user asked to generate; tagging alone is not permission. Linked-film video still needs scene_id.
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
    client = LLMClient()
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
    derived = session_log.derive_llm_history(events, max_user_turns=MAX_HISTORY_USER_TURNS)
    goal = next(
        (m["content"] for m in reversed(derived) if m.get("role") == "user"),
        "",
    )

    ws_result = await get_registry().execute(ctx, "get_workspace", {})
    summary = json.dumps(ws_result, ensure_ascii=False, default=str)
    if len(summary) > 3000:
        summary = summary[:3000] + "…[truncated]"

    plan = await _plan(goal, summary)
    if plan.get("mode") == "swarm" and ctx.project_id is None:
        # Swarm sub-agents need project-scoped tools; in a sandbox the single
        # loop handles create_project/link_project itself.
        plan["mode"] = "single"
        plan["tasks"] = []
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
    for i, task in enumerate(norm_tasks):
        role = task["role"]
        goal_i = task["goal"]
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

    # Synthesis: plain LLM call over sub-agent reports.
    synthesis_in = (
        "User goal:\n"
        f"{goal}\n\nSub-agent reports:\n" + "\n\n".join(results)
        + "\n\nWrite a concise final summary for the user: what was done, "
        "job ids enqueued, and any failures. Plain text."
    )
    client = LLMClient()
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

    client = LLMClient()
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
    final = ""
    try:
        for _ in range(max_iterations):
            payload = _scoped_payload(ctx, allowed_tools)
            msg = await client.chat_with_tools(
                [{"role": "system", "content": system}] + messages,
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
                    result = await registry.execute(ctx, name, parsed)
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
        else:
            final = "Reached step budget."
    finally:
        await client.close()
    return final or "Done."
