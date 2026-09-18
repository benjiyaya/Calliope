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


class _AwaitingInput(Exception):
    """Internal control flow: ask_user ended the turn awaiting the user."""

# Legacy constant kept for compatibility; the live default comes from
# settings.agent_max_steps (Settings → Queue tab) so users can raise it.
MAX_ITERATIONS = 12


def _default_max_iterations() -> int:
    try:
        return max(1, int(settings.agent_max_steps))
    except (TypeError, ValueError):
        return MAX_ITERATIONS


def _llm_for_role(role: str) -> LLMClient:
    # Tests patch loop.LLMClient with zero-arg fakes that lack for_role;
    # prefer role resolution, fall back to a bare instance.
    factory = LLMClient
    for_role = getattr(factory, "for_role", None)
    if callable(for_role):
        return for_role(role)
    return factory()


# ── Shared safety nets (main loop AND swarm sub-agents) ────────────────
# Extracted so orchestrator._run_sub_agent uses the exact same guards as
# run_turn — any future net added here applies to both loops for free.

REPEAT_EXECUTE_LIMIT = 2
FAIL_STREAK_LIMIT = 2

FINAL_STEP_NUDGE = (
    "[SYSTEM] This is your FINAL step of this turn. "
    "Do not start new tool work. Either (a) if the "
    "goal is already met, reply with a concise "
    "summary, or (b) reply with exactly what is "
    "blocked and why, and what you need to proceed. "
    "The user can continue the task in their next "
    "message."
)


class RepeatGuard:
    """Identical (tool, args) calls within one turn/loop.

    Read-like repeats execute normally (results may legitimately change);
    after REPEAT_EXECUTE_LIMIT the call is NOT re-executed — the cached real
    result is returned with an escalating instruction, so a stuck model gets
    its own data back plus a way out instead of burning the step budget.
    """

    def __init__(self) -> None:
        self.cache: dict[str, dict[str, Any]] = {}
        self.counts: dict[str, int] = {}

    def key(self, name: str, args: dict[str, Any]) -> str:
        return f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"

    def bumped(self, name: str, args: dict[str, Any]) -> int:
        k = self.key(name, args)
        self.counts[k] = self.counts.get(k, 0) + 1
        return self.counts[k]

    def cached(self, name: str, args: dict[str, Any]) -> dict[str, Any] | None:
        return self.cache.get(self.key(name, args))

    def intercept(self, name: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """Return the cached-result-plus-directive payload when over the
        limit, else None (caller should execute for real)."""
        count = self.bumped(name, args)
        if count > REPEAT_EXECUTE_LIMIT and self.key(name, args) in self.cache:
            cached = self.cache[self.key(name, args)]
            return {
                **cached,
                "repeat_guard": {
                    "calls_so_far": count,
                    "limit": REPEAT_EXECUTE_LIMIT,
                    "message": (
                        f"You have already called {name} with these exact "
                        f"{count} times this turn. This call was NOT executed "
                        "again — the result above is the real one from the last "
                        "execution. Do not repeat it again. If the goal is not "
                        "met, call a DIFFERENT tool, change the arguments "
                        "meaningfully, or end your turn with a summary/question "
                        "for the user."
                    ),
                },
            }
        return None

    def record(self, name: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        if isinstance(result, dict) and result.get("ok") is not False:
            self.cache[self.key(name, args)] = result


def fail_directive_text(streak: int) -> str:
    """The [SYSTEM DIRECTIVE] appended after FAIL_STREAK_LIMIT failures in a
    row — forces a DIFFERENT action or an explicit hand-back. Never a third
    blind try."""
    return (
        f"\n\n[SYSTEM DIRECTIVE] That is {streak} failed calls in a row on this "
        "goal. STOP retrying the same tool. Now either (a) call a DIFFERENT "
        "tool with corrected arguments, or (b) end your turn with a clear "
        "plain-text report of what is blocked and why, and what you need from "
        "the user. Do not call any tool again with the same arguments."
    )


def apply_fail_streak(result: dict[str, Any], streak: int) -> str:
    """Suffix builder: returns the directive only at/after the limit."""
    if streak >= FAIL_STREAK_LIMIT and isinstance(result, dict) and result.get("ok") is False:
        return fail_directive_text(streak)
    return ""

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

    # Repetition guard: identical (tool, args) calls within one turn. Read-like
    # repeats execute normally (results may legitimately change); after
    # REPEAT_EXECUTE_LIMIT the call is NOT re-executed — the cached real result
    # is returned with an escalating instruction, so a stuck model gets its
    # own data back plus a way out instead of burning the step budget.
    repeat = RepeatGuard()
    # Failure thrash guard (canvas/70): an agent that keeps getting errors
    # (guard denials, bad args) must CHANGE COURSE, not grind. After this
    # many not-ok results in a row, a directive is injected that forces a
    # different action or an explicit hand-back to the user.
    fail_streak = 0
    # Steering watermark: mid-run user course corrections appended after this
    # seq are drained into `messages` at step boundaries (never between an
    # assistant tool_calls message and its results — that would be an invalid
    # request sequence). Next turn's derivation replays the same events.
    steer_watermark = session_log.steering_max_seq(ctx.session_id)

    def drain_steering_into_messages() -> None:
        nonlocal steer_watermark
        for s in session_log.drain_steering(ctx.session_id, steer_watermark):
            steer_watermark = max(steer_watermark, s.seq)
            messages.append(
                {"role": "user", "content": session_log.steering_user_content(s.data)}
            )

    client = _llm_for_role("main")
    final_text = ""
    turn_status = "completed"
    synthetic_idx: list[int] = []  # positions of budget-nudge messages (stripped after)
    try:
        messages = [m for m in history if m.get("role") != "system"]
        for iteration in range(1, max_iterations + 1):
            # ── step boundary ────────────────────────────────────
            drain_steering_into_messages()
            log_append(session_log.STEP_START, {"turn": turn_no, "step": iteration})
            # Rebuilt per step: linking mid-run flips tool visibility and the
            # workspace digest changes after each tool.
            system = await prompts.assemble(ctx)
            remaining = max_iterations - iteration
            if remaining == 0:
                # Budget nudge: the FINAL step is dedicated to wrapping up
                # (answer or report), not starting work it cannot finish —
                # a turn that dies at the cap with no summary reads as
                # "agent did nothing".
                synthetic_idx.append(len(messages))
                messages.append(
                    {
                        "role": "user",
                        "content": FINAL_STEP_NUDGE,
                    }
                )
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
                    intercepted = repeat.intercept(name, args)
                    if intercepted is not None:
                        result = intercepted
                    else:
                        result = await registry.execute(ctx, name, args)
                        repeat.record(name, args, result)
                # Log + emit the result FIRST. The ask_user card and the
                # tool-row spinner both read these; ending the turn before
                # them left the UI with no question and a stuck "working…"
                # (the "agent loop just stops there" bug).
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
                # Thrash killer: a run of failures (guard denials, errors)
                # escalates — the appended directive forces a DIFFERENT
                # action or an explicit hand-back. Never a third blind try.
                if isinstance(result, dict) and result.get("ok") is False:
                    fail_streak += 1
                elif isinstance(result, dict):
                    fail_streak = 0
                result_text += apply_fail_streak(result, fail_streak)
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
                # ask_user ends the turn: the agent's question waits for the
                # user's answer, so the loop must not burn steps polling.
                if isinstance(result, dict) and result.get("awaiting_user_input"):
                    log_append(session_log.STEP_END, {"turn": turn_no, "step": iteration})
                    turn_status = "awaiting_input"
                    raise _AwaitingInput()
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
    except _AwaitingInput:
        # ask_user answered the turn's control flow: the tool result was
        # already appended above and the question event is in the log.
        pass
    except asyncio.CancelledError:
        turn_status = "cancelled"
        raise
    except Exception:
        turn_status = "failed"
        raise
    finally:
        await client.close()
        log_append(session_log.TURN_END, {"turn": turn_no, "status": turn_status})
        # Persist the exchange into history (the runner saves it to the DB),
        # minus the synthetic budget-nudge message — it belongs to THIS
        # turn's context only and would pollute the next turn.
        for idx in reversed(synthetic_idx):
            if 0 <= idx < len(messages) and messages[idx].get("content", "").startswith("[SYSTEM]"):
                del messages[idx]
        history.clear()
        history.extend(messages)
    return final_text


def _next_turn_number(session_id: int) -> int:
    return session_log.max_turn_number(session_id) + 1
