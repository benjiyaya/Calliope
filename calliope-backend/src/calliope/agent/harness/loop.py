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
            if count > REPEAT_EXECUTE_LIMIT + 1:
                return {
                    "ok": False,
                    "error": (
                        f"Tool '{name}' was called {count} times with identical arguments. "
                        "Call BLOCKED to prevent loop. You must choose a DIFFERENT tool "
                        "or produce your final answer now."
                    ),
                }
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


# ── Legacy JSON-action bridge (finding 6) ───────────────────────────────
# Some OpenAI-compatible endpoints (and older models) answer a tools payload
# with a prose/JSON action instead of a native tool_call. The bridge parses a
# {"tool": ..., "arguments": {...}} object out of the reply and executes it
# through the exact same guarded path — a parse loop can't spin forever, so
# bridged calls are capped per turn.

_BRIDGE_CAP_PER_TURN = 8

NO_FUNCTION_CALLING_NOTE = (
    "[This model answered with raw JSON instead of a tool call — its endpoint "
    "does not support function calling. Switch to a tool-calling model for "
    "agentic work.]"
)


def _extract_json_action(text: str) -> tuple[str, dict[str, Any]] | None:
    """Extract a ``{"tool": name, "arguments": {...}}`` action from model text.

    Prefers a fenced ```json block, then a bare/leading JSON object. Returns
    None when the text carries no parseable action — plain prose stays a final
    answer, never a bridge.
    """
    if not text or "{" not in text:
        return None

    def _action_from(candidate: str) -> tuple[str, dict[str, Any]] | None:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        name = parsed.get("tool") or parsed.get("name") or parsed.get("action")
        if not isinstance(name, str) or not name.strip():
            return None
        args = parsed.get("arguments", parsed.get("args", {}))
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return None
        return name.strip(), args

    # 1) fenced blocks first (```json ... ``` / ``` ... ```)
    if "```" in text:
        lines = text.splitlines()
        inside = False
        chunks: list[str] = []
        buf: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not inside and stripped.startswith("```"):
                inside = True
                continue
            if inside and stripped.startswith("```"):
                inside = False
                chunks.append("\n".join(buf))
                buf = []
                continue
            if inside:
                buf.append(line)
        if inside and buf:
            chunks.append("\n".join(buf))
        for chunk in chunks:
            action = _action_from(chunk.strip())
            if action is not None:
                return action

    # 2) first balanced {...} object anywhere in the text
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            parsed, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(parsed, dict):
            name = parsed.get("tool") or parsed.get("name") or parsed.get("action")
            if isinstance(name, str) and name.strip():
                args = parsed.get("arguments", parsed.get("args", {}))
                if args is None:
                    args = {}
                if isinstance(args, dict):
                    return name.strip(), args
        start = text.find("{", start + 1)
    return None


def _looks_like_json_reply(text: str) -> bool:
    """Heuristic for 'the model tried to answer in JSON but no action parsed'."""
    stripped = (text or "").strip()
    return stripped.startswith("{") or stripped.startswith("```")


# ── Read-only parallel dispatch (finding 8a) ────────────────────────────
# Provably read-only lookups: safe to run concurrently via asyncio.gather.
# Name-based allowlist plus a list_* prefix rule; everything NOT here (writes,
# generation, ask_user, run_command, wait_for_jobs) stays strictly sequential.
_READONLY_TOOLS = frozenset(
    {
        "get_workspace",
        "get_story",
        "get_job_status",
        "get_scene",
        "summarize_canvas",
        "read_skill",
        "list_skills",
        "list_memories",
        "comfy_server_info",
        "list_shot_presets",
        "list_poses",
        "list_projects",
        "list_jobs",
        "list_workflows",
    }
)


def _is_readonly_tool(name: str) -> bool:
    return name in _READONLY_TOOLS or name.startswith("list_")


def _usage_totals(usage: dict[str, Any]) -> dict[str, int] | None:
    """Normalize a server usage dict to prompt/completion/total counts.

    OpenAI-compatible servers report prompt_tokens/completion_tokens/total_tokens;
    total is derived when absent. None when nothing usable is present (no
    fabrication — absent means the server never reported it).
    """
    def _int(value: Any) -> int | None:
        try:
            out = int(value)
        except (TypeError, ValueError):
            return None
        return out if out >= 0 else None

    prompt = _int(usage.get("prompt_tokens"))
    completion = _int(usage.get("completion_tokens"))
    total = _int(usage.get("total_tokens"))
    if prompt is None and completion is None:
        return None
    if total is None:
        total = (prompt or 0) + (completion or 0)
    return {"prompt_tokens": prompt or 0, "completion_tokens": completion or 0, "total_tokens": total}

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

    def step_end_data(**extra: Any) -> dict[str, Any]:
        data: dict[str, Any] = {"turn": turn_no, "step": iteration, **extra}
        if step_usage is not None:
            data["usage"] = dict(step_usage)
        return data

    async def _log_call_start(tc: dict[str, Any], name: str, args: Any) -> None:
        """Persist the tool/call event + SSE start for one call. Shared by the
        sequential and parallel dispatch paths (parallel used to skip the
        call-side event, leaving tool/result rows with no matching record)."""
        raw_args = tc["function"].get("arguments") or "{}"
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

    async def _exec_one(tc: dict[str, Any]) -> tuple[str, Any, dict[str, Any]]:
        """Execute one native tool call (parse args, guards, execute).

        Returns (name, parsed_args_or_None, result) — logging/emitting and
        message-append stay with the caller so ordering and control flow
        (ask_user pause, fail streak) are identical on both dispatch paths.
        """
        name = tc["function"]["name"]
        raw_args = tc["function"].get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError:
            args = None
        if args is None:
            return name, None, {"ok": False, "error": f"Invalid JSON arguments for {name}: {raw_args[:200]}"}
        if not isinstance(args, dict):
            # Valid JSON but not an object (e.g. "[1,2,3]" / "\"hi\""):
            # tools call args.get(...) — reject with a self-correcting
            # message instead of an AttributeError traceback string.
            return (
                name,
                args,
                {
                    "ok": False,
                    "error": (
                        f"Tool arguments for {name} must be a JSON object, "
                        f"got {type(args).__name__}: {raw_args[:200]}"
                    ),
                },
            )
        intercepted = repeat.intercept(name, args)
        if intercepted is not None:
            return name, args, intercepted
        result = await registry.execute(ctx, name, args)
        repeat.record(name, args, result)
        return name, args, result

    async def _finish_call(
        tc: dict[str, Any], name: str, args: Any, result: dict[str, Any]
    ) -> None:
        """Log + emit one executed call and append its tool message. Shared by
        the sequential and parallel dispatch paths."""
        nonlocal fail_streak
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
                "name": name,
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

    client = _llm_for_role("main")
    final_text = ""
    turn_status = "completed"
    synthetic_idx: list[int] = []  # positions of budget-nudge messages (stripped after)
    bridged_count = 0  # JSON-action bridge executions this turn (cap _BRIDGE_CAP_PER_TURN)
    turn_usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    usage_seen = False  # only surface usage when the server actually reported it
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
            step_usage: dict[str, int] | None = None
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
                elif ev["type"] == "usage":
                    normalized = _usage_totals(ev["usage"] or {})
                    if normalized:
                        step_usage = normalized
                        usage_seen = True
                        for key, value in normalized.items():
                            turn_usage[key] = turn_usage.get(key, 0) + value

            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": ("".join(content_acc) or None),
            }
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        **{k: v for k, v in tc.items() if k != "function"},
                        "function": {
                            "name": tc["function"]["name"],
                            "arguments": tc["function"]["arguments"],
                        },
                    }
                    for tc in tool_calls
                ]
            messages.append(assistant_msg)

            if not tool_calls:
                # ── Legacy JSON-action bridge (finding 6) ────────────
                # Zero native tool_calls but a tools payload WAS sent: the
                # endpoint may have answered with a prose/JSON action. Parse
                # one out and run it through the exact same guarded exec path
                # (synthetic call id bridge_{n}); plain prose is untouched.
                step_text = "".join(content_acc)
                action = _extract_json_action(step_text) if tools else None
                if action is not None and bridged_count < _BRIDGE_CAP_PER_TURN:
                    bridged_count += 1
                    bridge_name, bridge_args = action
                    raw_args = json.dumps(bridge_args, ensure_ascii=False)
                    call_id = f"bridge_{turn_no}_{bridged_count}"
                    # Persist the assistant message WITH its synthetic tool_calls
                    # so derive_llm_history replays the same valid exchange the
                    # live loop built (mirrors the native batch path above).
                    log_append(
                        session_log.ASSISTANT_MESSAGE,
                        {
                            "turn": turn_no,
                            "step": iteration,
                            "content": step_text.strip() or None,
                            "agent_name": agent_name,
                            "tool_calls": [
                                {
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": bridge_name,
                                        "arguments": raw_args,
                                    },
                                }
                            ],
                        },
                    )
                    log_append(
                        session_log.TOOL_CALL,
                        {
                            "turn": turn_no,
                            "step": iteration,
                            "call_id": call_id,
                            "tool_name": bridge_name,
                            "arguments": raw_args,
                            "agent_name": agent_name,
                            "bridged": True,
                        },
                    )
                    await event_bus.publish(
                        "agent.tool",
                        {
                            "session_id": ctx.session_id,
                            "agent_name": agent_name,
                            "phase": "start",
                            "tool": bridge_name,
                            "args": bridge_args,
                        },
                    )
                    if registry.get(bridge_name) is None:
                        result = {
                            "ok": False,
                            "error": f"Unknown tool: {bridge_name}",
                        }
                    else:
                        intercepted = repeat.intercept(bridge_name, bridge_args)
                        if intercepted is not None:
                            result = intercepted
                        else:
                            result = await registry.execute(ctx, bridge_name, bridge_args)
                            repeat.record(bridge_name, bridge_args, result)
                    log_append(
                        session_log.TOOL_RESULT,
                        {
                            "turn": turn_no,
                            "step": iteration,
                            "call_id": call_id,
                            "tool_name": bridge_name,
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
                            "tool": bridge_name,
                            "result": result,
                        },
                    )
                    result_text = json.dumps(result, ensure_ascii=False, default=str)
                    if len(result_text) > session_log.TOOL_RESULT_TRUNCATE:
                        result_text = result_text[: session_log.TOOL_RESULT_TRUNCATE] + session_log.TRUNCATE_NOTE
                    if isinstance(result, dict) and result.get("ok") is False:
                        fail_streak += 1
                    elif isinstance(result, dict):
                        fail_streak = 0
                    result_text += apply_fail_streak(result, fail_streak)
                    # Rewrite the assistant message into a proper tool-call
                    # exchange so derive_llm_history replays a valid sequence.
                    messages.pop()
                    assistant_msg = {
                        "role": "assistant",
                        "content": ("".join(content_acc) or None),
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": bridge_name,
                                    "arguments": raw_args,
                                },
                            }
                        ],
                    }
                    messages.append(assistant_msg)
                    messages.append(
                        {"role": "tool", "tool_call_id": call_id, "content": result_text}
                    )
                    await emit(
                        {
                            "role": "tool",
                            "agent_name": agent_name,
                            "tool_name": bridge_name,
                            "tool_args": bridge_args,
                            "tool_result": result,
                            "content": "",
                        }
                    )
                    # ask_user via the bridge pauses the turn like a native one.
                    if isinstance(result, dict) and result.get("awaiting_user_input"):
                        log_append(session_log.STEP_END, step_end_data())
                        turn_status = "awaiting_input"
                        raise _AwaitingInput()
                    log_append(session_log.STEP_END, step_end_data(bridged=True))
                    continue  # next step: the model sees its tool result
                if action is not None:
                    # Bridge cap reached: stop the parse loop deterministically.
                    final_text = (
                        "I paused: this turn executed the maximum number of "
                        f"bridged tool calls ({_BRIDGE_CAP_PER_TURN}). Send another "
                        "message to continue — or switch to a tool-calling "
                        "model, which does not have this limit."
                    )
                    log_append(
                        session_log.ASSISTANT_MESSAGE,
                        {"turn": turn_no, "step": iteration, "content": final_text, "agent_name": agent_name},
                    )
                    log_append(session_log.STEP_END, step_end_data())
                    await emit({"role": "assistant", "agent_name": agent_name, "content": final_text})
                    break
                final_text = "".join(content_acc).strip()
                if final_text and _looks_like_json_reply(final_text) and tools:
                    # Raw JSON that parsed as neither a native call nor a
                    # bridge action: the endpoint does not support function
                    # calling — tell the user instead of a silent no-op.
                    final_text = final_text + "\n\n" + NO_FUNCTION_CALLING_NOTE
                reasoning_text = "".join(reasoning_acc).strip() or None
                msg_data: dict[str, Any] = {"turn": turn_no, "step": iteration, "content": final_text, "agent_name": agent_name}
                if reasoning_text:
                    msg_data["reasoning"] = reasoning_text
                log_append(session_log.ASSISTANT_MESSAGE, msg_data)
                log_append(session_log.STEP_END, step_end_data())
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

            # ── Read-only parallel dispatch (finding 8a) ────────────────
            # A batch of >1 calls runs concurrently ONLY when every call's
            # tool is a provably read-only lookup — no writes, no approvals,
            # no shell. Everything else keeps today's exact sequential
            # behavior (execute → log → emit per call, in order).
            parallel = len(tool_calls) > 1 and all(
                _is_readonly_tool(tc["function"]["name"]) for tc in tool_calls
            )
            if parallel:
                # Call-side events first, in order, then gather — every
                # tool/result keeps a matching tool/call record.
                for tc in tool_calls:
                    await _log_call_start(tc, tc["function"]["name"], None)
                exec_results: list[tuple[str, Any, dict[str, Any]]] = list(
                    await asyncio.gather(*(_exec_one(tc) for tc in tool_calls))
                )
                for tc, (name, args, result) in zip(tool_calls, exec_results):
                    await _finish_call(tc, name, args, result)
                log_append(session_log.STEP_END, step_end_data(parallel=True))
                continue
            for tc_idx, tc in enumerate(tool_calls):
                name = tc["function"]["name"]
                raw_args = tc["function"].get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    args = None
                await _log_call_start(tc, name, args)
                name, args, result = await _exec_one(tc)
                # Log + emit the result FIRST. The ask_user card and the
                # tool-row spinner both read these; ending the turn before
                # them left the UI with no question and a stuck "working…"
                # (the "agent loop just stops there" bug).
                await _finish_call(tc, name, args, result)
                # ask_user ends the turn: the agent's question waits for the
                # user's answer, so the loop must not burn steps polling.
                if isinstance(result, dict) and result.get("awaiting_user_input"):
                    # Close the batch first: any REMAINING calls of this step
                    # never executed, and an assistant tool_calls message with
                    # missing results is an invalid request sequence (strict
                    # OpenAI-compatible servers 400 the next turn). Synthesize
                    # skipped receipts for the un-executed call ids.
                    for pending in tool_calls[tc_idx + 1:]:
                        skipped = {
                            "ok": False,
                            "skipped": True,
                            "error": (
                                "Not executed — this turn paused for the user's "
                                "answer to an ask_user question in the same step. "
                                "Re-issue the call after they answer if still needed."
                            ),
                        }
                        pname = pending["function"]["name"]
                        praw_args = pending["function"].get("arguments") or "{}"
                        try:
                            pargs = json.loads(praw_args) if praw_args.strip() else None
                        except json.JSONDecodeError:
                            pargs = None
                        log_append(
                            session_log.TOOL_CALL,
                            {
                                "turn": turn_no,
                                "step": iteration,
                                "call_id": pending["id"],
                                "tool_name": pname,
                                "arguments": praw_args,
                                "agent_name": agent_name,
                            },
                        )
                        log_append(
                            session_log.TOOL_RESULT,
                            {
                                "turn": turn_no,
                                "step": iteration,
                                "call_id": pending["id"],
                                "tool_name": pname,
                                "result": skipped,
                                "agent_name": agent_name,
                            },
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": pending["id"],
                                "content": json.dumps(skipped, ensure_ascii=False),
                            }
                        )
                        await emit(
                            {
                                "role": "tool",
                                "agent_name": agent_name,
                                "tool_name": pname,
                                "tool_args": pargs if isinstance(pargs, dict) else None,
                                "tool_result": skipped,
                                "content": "",
                            }
                        )
                    log_append(session_log.STEP_END, step_end_data())
                    turn_status = "awaiting_input"
                    raise _AwaitingInput()
            log_append(session_log.STEP_END, step_end_data())
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
        turn_end_data: dict[str, Any] = {"turn": turn_no, "status": turn_status}
        if usage_seen:
            turn_end_data["usage"] = dict(turn_usage)
        log_append(session_log.TURN_END, turn_end_data)
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
