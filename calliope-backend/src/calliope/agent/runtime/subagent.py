"""One scoped subagent. It can call the tools in its grant and nothing else."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from calliope.agent.capabilities.catalog import GRANTS, execute, prompt_for, tools_for
from calliope.agent.llm import LLMClient, ToolCall
from calliope.agent.media import project_user_content
from calliope.agent.runtime.types import StreamSink
from calliope.config import settings

# ── JSON-action bridge (no-function-calling endpoints) ────────────────────
# When a step returns zero tool_calls but a tools payload WAS sent, a fenced
# ```json {"tool": name, "arguments": {...}} block (or a bare object with the
# same keys) is executed as a REAL call through the same path — grant check,
# catalog.execute, on_tool audit. Cap stops parse loops.
_BRIDGE_CAP_PER_TURN = 8
_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_NO_FUNCTION_CALLING_NOTE = (
    "\n\n[Note: function calling appears unavailable on this endpoint. To "
    "call a tool, reply ONLY with a fenced json block: {\"tool\": "
    "\"<tool name>\", \"arguments\": {…}} — it will be executed.]"
)


def _bridge_action(text: str, index: int) -> ToolCall | None:
    """Parse one {tool, arguments} action from a reply. None when absent."""
    if not text:
        return None
    fenced = _JSON_BLOCK_RE.search(text)
    if fenced:
        candidates = [fenced.group(1)]
    else:
        start = text.find("{")
        end = text.rfind("}")
        candidates = [text[start : end + 1]] if start != -1 and end > start else []
    for raw in candidates:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        name = parsed.get("tool") or parsed.get("name")
        arguments = parsed.get("arguments")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(arguments, dict):
            arguments = {}
        return ToolCall(id=f"bridge_{index}", name=name, arguments=arguments)
    return None


def _looks_like_json_action(text: str) -> bool:
    stripped = (text or "").lstrip()
    return stripped.startswith("{") or stripped.startswith("```json")


@dataclass
class SubagentResult:
    text: str
    tools_ran: list[str] = field(default_factory=list)
    finished: bool = False
    paused: bool = False
    # Set when create_project links this chat. The coordinator adopts it
    # before the next fingerprint, so later calls write into the new project.
    linked_project_id: int | None = None
    # The ReAct transcript, including tool results. A later attempt continues
    # from this instead of forgetting what it already observed.
    transcript: list[dict[str, Any]] = field(default_factory=list)


def _tool_calls(message: dict[str, Any]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") or {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                parsed = {}
        elif isinstance(arguments, dict):
            parsed = arguments
        else:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        calls.append(
            ToolCall(
                id=str(raw.get("id") or f"call_{index}"),
                name=str(function.get("name") or ""),
                arguments=parsed,
            )
        )
    return calls


async def run_subagent(
    *,
    project_id: int | None,
    session_id: int | None,
    grant: str,
    goal: str,
    thinking: str | None = None,
    cancel: asyncio.Event | None = None,
    pull_steering: Callable[[], list[str]] | None = None,
    on_tool: Callable[[str, dict[str, Any], dict[str, Any]], Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    stream: StreamSink | None = None,
    attachments: list[dict[str, Any]] | None = None,
    mentions: list[dict[str, Any]] | None = None,
) -> SubagentResult:
    effort = None if thinking in (None, "", "default") else thinking
    client = LLMClient() if effort is None else LLMClient(thinking=effort)
    tools_ran: list[str] = []
    linked_project_id: int | None = None
    waiting = False
    granted = GRANTS.get(grant, GRANTS["script"])
    if history:
        messages = list(history)
    else:
        from calliope.agent.capabilities.memory import recall_text

        system = prompt_for(grant)
        if grant != "shot":
            from calliope.agent.capabilities.workspace import files_prompt_text

            files_section = files_prompt_text(session_id)
            if files_section:
                system = f"{system}\n\n{files_section}"
        remembered = recall_text(project_id)
        if remembered:
            system = f"{system}\n\n{remembered}"
        hardening = (settings.agent_hardening_prompt or "").strip()
        if hardening:
            # Operator-editable hardening rules render LAST — the final word
            # before the transcript (same position the plugin harness used).
            system = f"{system}\n\n{hardening}"
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": project_user_content(goal, mentions, attachments)},
        ]
    bridge_used = 0
    try:
        steps = max(1, int(settings.agent_max_steps or 1))
        for _step in range(steps):
            if cancel is not None and cancel.is_set():
                raise asyncio.CancelledError
            if pull_steering is not None:
                for note in pull_steering():
                    messages.append({"role": "user", "content": f"[STEERING] {note}"})
            message = await client.chat_with_tools(
                messages,
                tools=tools_for(grant),
                on_token=stream.token if stream is not None else None,
                on_reasoning=stream.reasoning if stream is not None else None,
                on_usage=stream.usage if stream is not None else None,
            )
            calls = _tool_calls(message)
            if not calls:
                # JSON-action bridge: a no-function-calling model can still
                # act by writing a {"tool", "arguments"} block. The action
                # goes through the SAME exec path as a real tool call.
                reply = str(message.get("content") or "")
                if bridge_used < _BRIDGE_CAP_PER_TURN:
                    action = _bridge_action(reply, bridge_used + 1)
                    if action is not None:
                        bridge_used += 1
                        message = {
                            "role": "assistant",
                            "content": reply,
                            "tool_calls": [
                                {
                                    "id": action.id,
                                    "type": "function",
                                    "function": {
                                        "name": action.name,
                                        "arguments": json.dumps(action.arguments),
                                    },
                                }
                            ],
                        }
                        calls = [action]
            if not calls:
                reply = str(message.get("content") or "")
                if _looks_like_json_action(reply):
                    # Raw JSON that parses as no action: teach the way out
                    # instead of leaving the model (and user) guessing.
                    reply += _NO_FUNCTION_CALLING_NOTE
                return SubagentResult(
                    text=reply,
                    tools_ran=tools_ran,
                    finished=True,
                    linked_project_id=linked_project_id,
                    transcript=list(messages),
                )
            messages.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": message.get("tool_calls") or [],
                }
            )
            for call in calls:
                tools_ran.append(call.name)
                if call.name not in granted:
                    result = {"ok": False, "error": f"{call.name} is not granted"}
                else:
                    try:
                        result = await execute(call.name, call.arguments, project_id, session_id)
                    except Exception as exc:
                        result = {"ok": False, "error": str(exc)}
                if (
                    call.name in ("create_project", "link_project")
                    and result.get("ok")
                    and result.get("project_id") is not None
                ):
                    project_id = int(result["project_id"])
                    linked_project_id = project_id
                if on_tool is not None:
                    maybe = on_tool(call.name, call.arguments, result)
                    if asyncio.iscoroutine(maybe):
                        await maybe
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result),
                    }
                )
                if result.get("awaiting_review") or result.get("awaiting_input"):
                    waiting = True
                    break
            if waiting:
                return SubagentResult(
                    text=str(message.get("content") or ""),
                    tools_ran=tools_ran,
                    finished=False,
                    paused=True,
                    linked_project_id=linked_project_id,
                    transcript=list(messages),
                )
        return SubagentResult(
            text="",
            tools_ran=tools_ran,
            finished=False,
            linked_project_id=linked_project_id,
            transcript=list(messages),
        )
    finally:
        await client.close()
