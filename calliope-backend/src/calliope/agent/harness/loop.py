"""The agentic loop: a turn/step driver over the session event log.

Ported from the deepseek-harness driver shape:
- One TURN per user message; each turn contains STEPs (one model request +
  its tool calls each). Every boundary is appended to the event log.
- LLM history is DERIVED from the log before each step (derive_llm_history),
  so persistence and replay share one source of truth.
- Assistant tokens stream as `agent.token` events; tool executions publish
  `agent.tool` start/finish + tool/call + tool/result log events.

The public contract `run_turn(ctx, history, ...)` is kept for compatibility:
it still mutates `history` in place and returns the final text.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from calliope.agent.harness import get_prompts, get_registry
from calliope.agent.harness import log as session_log
from calliope.agent.harness.registry import ToolContext
from calliope.agent.llm import LLMClient
from calliope.config import settings
from calliope.events.bus import event_bus

logger = logging.getLogger("calliope.harness.loop")

# Legacy constant kept for compatibility; the live default comes from
# settings.agent_max_steps (Settings → Queue tab) so users can raise it.
MAX_ITERATIONS = 12


def _default_max_iterations() -> int:
    try:
        return max(1, int(settings.agent_max_steps))
    except (TypeError, ValueError):
        return MAX_ITERATIONS

# Async callback that persists + broadcasts one harness message.
MessageSink = Callable[[dict[str, Any]], Awaitable[None]]


async def run_turn(
    ctx: ToolContext,
    history: list[dict[str, Any]],
    *,
    agent_name: str | None = None,
    max_iterations: int | None = None,
    on_message: MessageSink | None = None,
) -> str:
    """Run one agentic turn.

    `history` is mutated in place with the full multi-turn exchange (assistant
    tool_calls + tool results + final answer) — compatibility contract.

    Returns the final assistant text. Raises asyncio.CancelledError when the
    run is cancelled so the runner can persist the interruption.
    """
    if max_iterations is None:
        max_iterations = _default_max_iterations()
    max_iterations = max(1, int(max_iterations))
    registry = get_registry()
    prompts = get_prompts()

    async def emit(message: dict[str, Any]) -> None:
        if on_message:
            await on_message({"session_id": ctx.session_id, **message})
        else:
            await event_bus.publish("agent.message", {"session_id": ctx.session_id, **message})

    def log_append(event_type: str, data: dict[str, Any]) -> session_log.SessionEvent:
        return session_log.append_event(ctx.session_id, event_type, data)

    # ── turn boundary ───────────────────────────────────────────
    turn_no = _next_turn_number(ctx.session_id)
    log_append(session_log.TURN_START, {"turn": turn_no})

    client = LLMClient()
    final_text = ""
    turn_status = "completed"
    try:
        messages = [m for m in history if m.get("role") != "system"]
        for iteration in range(1, max_iterations + 1):
            # ── step boundary ────────────────────────────────────
            log_append(session_log.STEP_START, {"turn": turn_no, "step": iteration})
            # Rebuilt per step: linking mid-run flips tool visibility and the
            # workspace digest changes after each tool.
            system = await prompts.assemble(ctx)
            tools = registry.openai_payload(ctx)
            stream = client.chat_stream(
                [{"role": "system", "content": system}] + messages,
                temperature=0.4,
                tools=tools or None,
            )
            content_acc: list[str] = []
            reasoning_acc: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            async for ev in stream:
                if ev["type"] == "delta":
                    content_acc.append(ev["content"])
                    await event_bus.publish(
                        "agent.token",
                        {
                            "session_id": ctx.session_id,
                            "agent_name": agent_name,
                            "content": ev["content"],
                        },
                    )
                elif ev["type"] == "reasoning":
                    reasoning_acc.append(ev["content"])
                    await event_bus.publish(
                        "agent.thinking",
                        {
                            "session_id": ctx.session_id,
                            "agent_name": agent_name,
                            "content": ev["content"],
                        },
                    )
                elif ev["type"] == "tool_call":
                    tool_calls.append(ev["tool_call"])

            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": ("".join(content_acc) or None),
            }
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["function"]["name"],
                            "arguments": tc["function"]["arguments"],
                        },
                    }
                    for tc in tool_calls
                ]
            messages.append(assistant_msg)

            if not tool_calls:
                final_text = "".join(content_acc).strip()
                reasoning_text = "".join(reasoning_acc).strip() or None
                msg_data: dict[str, Any] = {"turn": turn_no, "step": iteration, "content": final_text, "agent_name": agent_name}
                if reasoning_text:
                    msg_data["reasoning"] = reasoning_text
                log_append(session_log.ASSISTANT_MESSAGE, msg_data)
                log_append(session_log.STEP_END, {"turn": turn_no, "step": iteration})
                # Emit the final assistant message as an SSE event so the
                # frontend can display it immediately (without waiting for
                # the next poll cycle).
                emit_payload: dict[str, Any] = {
                    "role": "assistant",
                    "agent_name": agent_name,
                    "content": final_text,
                }
                if reasoning_text:
                    emit_payload["reasoning"] = reasoning_text
                await emit(emit_payload)
                break

            # Model produced tool calls: record the assistant message, then
            # execute each call and append results.
            tool_msg_data: dict[str, Any] = {
                "turn": turn_no,
                "step": iteration,
                "content": "".join(content_acc).strip() or None,
                "agent_name": agent_name,
                "tool_calls": assistant_msg["tool_calls"],
            }
            reasoning_text = "".join(reasoning_acc).strip() or None
            if reasoning_text:
                tool_msg_data["reasoning"] = reasoning_text
            log_append(session_log.ASSISTANT_MESSAGE, tool_msg_data)
            for tc in tool_calls:
                name = tc["function"]["name"]
                raw_args = tc["function"].get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    args = None
                log_append(
                    session_log.TOOL_CALL,
                    {
                        "turn": turn_no,
                        "step": iteration,
                        "call_id": tc["id"],
                        "tool_name": name,
                        "arguments": raw_args,
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
                        "args": args,
                    },
                )
                if args is None:
                    result = {
                        "ok": False,
                        "error": f"Invalid JSON arguments for {name}: {raw_args[:200]}",
                    }
                elif not isinstance(args, dict):
                    # Valid JSON but not an object (e.g. "[1,2,3]" / "\"hi\""):
                    # tools call args.get(...) — reject with a self-correcting
                    # message instead of an AttributeError traceback string.
                    result = {
                        "ok": False,
                        "error": (
                            f"Tool arguments for {name} must be a JSON object, "
                            f"got {type(args).__name__}: {raw_args[:200]}"
                        ),
                    }
                else:
                    result = await registry.execute(ctx, name, args)
                log_append(
                    session_log.TOOL_RESULT,
                    {
                        "turn": turn_no,
                        "step": iteration,
                        "call_id": tc["id"],
                        "tool_name": name,
                        "result": result,
                        "agent_name": agent_name,
                    },
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
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result_text,
                    }
                )
                await emit(
                    {
                        "role": "tool",
                        "agent_name": agent_name,
                        "tool_name": name,
                        "tool_args": args,
                        "tool_result": result,
                        "content": "",
                    }
                )
            log_append(session_log.STEP_END, {"turn": turn_no, "step": iteration})
        else:
            final_text = (
                "I reached my step budget for this turn. Here is where things "
                "stand — send another message to continue."
            )
            log_append(
                session_log.ASSISTANT_MESSAGE,
                {"turn": turn_no, "step": max_iterations, "content": final_text, "agent_name": agent_name},
            )
            await emit(
                {
                    "role": "assistant",
                    "agent_name": agent_name,
                    "content": final_text,
                }
            )
            turn_status = "step_budget_exhausted"
    except asyncio.CancelledError:
        turn_status = "cancelled"
        raise
    except Exception:
        turn_status = "failed"
        raise
    finally:
        await client.close()
        log_append(session_log.TURN_END, {"turn": turn_no, "status": turn_status})
        # Persist the exchange into history (the runner saves it to the DB).
        history.clear()
        history.extend(messages)
    return final_text


def _next_turn_number(session_id: int) -> int:
    return session_log.max_turn_number(session_id) + 1
