"""Legacy JSON-action bridge + read-only parallel dispatch (loop.run_turn).

Bridge: a no-function-calling model answering with a {"tool": ...} JSON
action still executes through the same guarded path, capped per turn; raw
JSON that parses as no action gets the no-function-calling note.
Parallel: a read-only batch gathers, mixed batches stay sequential, ask_user
is never parallel.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from calliope.agent.harness import loop as loop_mod
from calliope.agent.harness import log as session_log
from calliope.agent.harness.loop import (
    NO_FUNCTION_CALLING_NOTE,
    _BRIDGE_CAP_PER_TURN,
    _extract_json_action,
    _is_readonly_tool,
)
from calliope.agent.harness.registry import ToolContext
from calliope.config import settings
from calliope.db import get_db


@pytest.fixture(autouse=True, scope="module")
def _never_touch_real_db():
    prev = settings.data_dir
    with tempfile.TemporaryDirectory() as tmp:
        settings.data_dir = Path(tmp)
        settings.assets_dir = Path(tmp) / "assets"
        asyncio.run(_migrate(settings.db_path))
        yield
    settings.data_dir = prev


async def _migrate(db_path):
    from calliope.db import migrate_db

    await migrate_db(db_path)


def _mk_session(title: str = "loop-bridge") -> int:
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO agent_sessions (title, project_id) VALUES (?, NULL)", (title,)
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _tc(name: str, args: str = "{}", call_id: str = "c1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


class _ScriptedStreamClient:
    """chat_stream replays scripted replies; each reply is a raw assistant msg."""

    def __init__(self, replies):
        self._replies = list(replies)

    def chat_stream(self, messages, temperature=0.4, tools=None, tool_choice=None):
        reply = self._replies.pop(0) if self._replies else {
            "role": "assistant", "content": "done", "tool_calls": []
        }
        return self._stream(reply)

    async def _stream(self, reply):
        if reply.get("content"):
            yield {"type": "delta", "content": reply["content"]}
        for tc in reply.get("tool_calls") or []:
            yield {"type": "tool_call", "tool_call": tc}
        yield {"type": "done"}

    async def close(self):
        pass


class _StubRegistry:
    def __init__(self, results_by_tool=None, payload_tools=None):
        self.executed: list[tuple[str, dict]] = []
        self.results_by_tool = results_by_tool or {}
        self.payload_tools = payload_tools or []

    def openai_payload(self, ctx):
        return [
            {"type": "function", "function": {"name": n, "description": "", "parameters": {}}}
            for n in self.payload_tools
        ]

    def get(self, name):
        if name in self.payload_tools:
            return {"name": name}
        return None

    async def execute(self, ctx, name, args):
        self.executed.append((name, dict(args)))
        r = self.results_by_tool.get(name)
        if r is not None:
            return dict(r)
        if name == "ask_user":
            return {"ok": True, "awaiting_user_input": True, "question": "q?"}
        return {"ok": True}


def _run_turn_with(sid, client, registry, replies=None):
    orig_client, orig_registry = loop_mod.LLMClient, loop_mod.get_registry
    loop_mod.LLMClient = lambda *a, **k: client
    loop_mod.get_registry = lambda: registry
    try:
        ctx = ToolContext(session_id=sid, project_id=None)
        history = session_log.derive_llm_history(session_log.read_events(sid))
        final = asyncio.run(loop_mod.run_turn(ctx, history))
    finally:
        loop_mod.LLMClient, loop_mod.get_registry = orig_client, orig_registry
    return final


def _events_of(sid):
    return session_log.read_events(sid)


# ── _extract_json_action ─────────────────────────────────────────────────


def test_extract_fenced_action():
    text = 'I will do it.\n```json\n{"tool": "get_workspace", "arguments": {"sections": ["beats"]}}\n```'
    assert _extract_json_action(text) == ("get_workspace", {"sections": ["beats"]})


def test_extract_bare_object_action():
    text = '{"tool": "get_story", "arguments": {}}'
    assert _extract_json_action(text) == ("get_story", {})


def test_extract_returns_none_for_prose():
    assert _extract_json_action("The project has 3 characters and 2 scenes.") is None
    assert _extract_json_action("") is None


def test_extract_rejects_non_dict_arguments():
    text = '{"tool": "get_story", "arguments": "not-a-dict"}'
    assert _extract_json_action(text) is None


# ── bridge executes through the guarded path ─────────────────────────────


def test_bridge_executes_action_and_logs_events():
    sid = _mk_session()
    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "go"})
    client = _ScriptedStreamClient(
        [
            {"content": '```json\n{"tool": "get_workspace", "arguments": {}}\n```', "tool_calls": []},
            {"content": "All done.", "tool_calls": []},
        ]
    )
    registry = _StubRegistry(
        {"get_workspace": {"ok": True}}, payload_tools=["get_workspace"]
    )
    final = _run_turn_with(sid, client, registry)
    assert final == "All done."
    assert [n for n, _ in registry.executed] == ["get_workspace"]
    events = _events_of(sid)
    tool_calls = [e for e in events if e.type == session_log.TOOL_CALL]
    assert tool_calls and tool_calls[0].data["call_id"].startswith("bridge_")
    assert tool_calls[0].data.get("bridged") is True
    # ASSISTANT_MESSAGE carries the synthetic tool_calls for history derivation.
    assistant = [e for e in events if e.type == session_log.ASSISTANT_MESSAGE]
    assert any(
        (e.data.get("tool_calls") or []) and e.data["tool_calls"][0]["id"].startswith("bridge_")
        for e in assistant
    )
    # step/end marks the bridged step.
    step_ends = [e for e in events if e.type == session_log.STEP_END]
    assert any(e.data.get("bridged") is True for e in step_ends)


def test_bridge_guard_denial_applies():
    """A bridged requires_approval call is denied like a native one — the
    bridge is transport, not a permission bypass."""
    sid = _mk_session()
    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "render"})
    denial = {"ok": False, "reason_code": "guard_render_approval", "error": "blocked"}
    client = _ScriptedStreamClient(
        [
            {"content": '{"tool": "enqueue_video_jobs", "arguments": {"scene_ids": [1]}}', "tool_calls": []},
            {"content": "gave up", "tool_calls": []},
        ]
    )
    registry = _StubRegistry({"enqueue_video_jobs": denial}, payload_tools=["enqueue_video_jobs"])
    _run_turn_with(sid, client, registry)
    results = [e for e in _events_of(sid) if e.type == session_log.TOOL_RESULT]
    assert results[0].data["result"].get("reason_code") == "guard_render_approval"


def test_bridge_cap_stops_parse_loop():
    sid = _mk_session()
    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "go"})
    # Distinct args per reply so the RepeatGuard stays out of the way and the
    # test isolates the bridge cap itself.
    replies = [
        {"content": json.dumps({"tool": "get_workspace", "arguments": {"n": i}}), "tool_calls": []}
        for i in range(_BRIDGE_CAP_PER_TURN + 5)
    ]
    client = _ScriptedStreamClient(replies)
    registry = _StubRegistry({"get_workspace": {"ok": True}}, payload_tools=["get_workspace"])
    final = _run_turn_with(sid, client, registry)
    assert len(registry.executed) == _BRIDGE_CAP_PER_TURN
    assert "maximum number of bridged tool calls" in final


def test_raw_json_without_action_gets_no_fc_note():
    sid = _mk_session()
    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "hi"})
    client = _ScriptedStreamClient(
        [{"content": '{"status": "ok", "items": []}', "tool_calls": []}]
    )
    registry = _StubRegistry(payload_tools=["get_workspace"])
    final = _run_turn_with(sid, client, registry)
    assert NO_FUNCTION_CALLING_NOTE in final


def test_plain_prose_final_untouched():
    sid = _mk_session()
    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "hi"})
    client = _ScriptedStreamClient([{"content": "The project has 2 scenes.", "tool_calls": []}])
    registry = _StubRegistry(payload_tools=["get_workspace"])
    final = _run_turn_with(sid, client, registry)
    assert final == "The project has 2 scenes."
    assert NO_FUNCTION_CALLING_NOTE not in final


# ── read-only parallel dispatch ──────────────────────────────────────────


def test_readonly_allowlist_membership():
    assert _is_readonly_tool("get_workspace")
    assert _is_readonly_tool("list_workflows")  # list_ prefix
    assert _is_readonly_tool("read_skill")
    assert not _is_readonly_tool("run_command")
    assert not _is_readonly_tool("enqueue_video_jobs")
    assert not _is_readonly_tool("ask_user")
    assert not _is_readonly_tool("save_memory")  # a write
    assert not _is_readonly_tool("wait_for_jobs")


def test_readonly_batch_runs_parallel_and_preserves_order():
    sid = _mk_session("loop-parallel")
    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "inspect"})
    client = _ScriptedStreamClient(
        [
            {
                "content": None,
                "tool_calls": [
                    _tc("get_workspace", call_id="p1"),
                    _tc("list_workflows", call_id="p2"),
                    _tc("list_memories", call_id="p3"),
                ],
            },
            {"content": "done", "tool_calls": []},
        ]
    )
    registry = _StubRegistry(
        payload_tools=["get_workspace", "list_workflows", "list_memories"]
    )
    _run_turn_with(sid, client, registry)
    events = _events_of(sid)
    step_ends = [e for e in events if e.type == session_log.STEP_END]
    assert any(e.data.get("parallel") is True for e in step_ends)
    results = [e for e in events if e.type == session_log.TOOL_RESULT]
    assert [e.data["call_id"] for e in results] == ["p1", "p2", "p3"]


def test_parallel_batch_logs_call_and_result_pairs():
    """Every parallel tool/result keeps a matching tool/call event (same
    contract as the sequential path — the audit log never has orphaned
    results)."""
    sid = _mk_session("loop-parallel-pairs")
    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "inspect"})
    client = _ScriptedStreamClient(
        [
            {
                "content": None,
                "tool_calls": [
                    _tc("get_workspace", call_id="c1"),
                    _tc("list_workflows", call_id="c2"),
                ],
            },
            {"content": "done", "tool_calls": []},
        ]
    )
    registry = _StubRegistry(payload_tools=["get_workspace", "list_workflows"])
    _run_turn_with(sid, client, registry)
    events = _events_of(sid)
    calls = [e for e in events if e.type == session_log.TOOL_CALL]
    results = [e for e in events if e.type == session_log.TOOL_RESULT]
    assert [e.data["call_id"] for e in calls] == ["c1", "c2"]
    assert [e.data["call_id"] for e in results] == ["c1", "c2"]


def test_mixed_batch_stays_sequential():
    sid = _mk_session("loop-mixed")
    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "do"})
    client = _ScriptedStreamClient(
        [
            {
                "content": None,
                "tool_calls": [
                    _tc("get_workspace", call_id="m1"),
                    _tc("save_memory", call_id="m2"),
                ],
            },
            {"content": "done", "tool_calls": []},
        ]
    )
    registry = _StubRegistry(payload_tools=["get_workspace", "save_memory"])
    _run_turn_with(sid, client, registry)
    step_ends = [e for e in _events_of(sid) if e.type == session_log.STEP_END]
    assert not any(e.data.get("parallel") for e in step_ends), (
        "a batch containing a write must stay sequential"
    )


def test_single_call_batch_stays_sequential():
    sid = _mk_session("loop-single")
    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "do"})
    client = _ScriptedStreamClient(
        [
            {"content": None, "tool_calls": [_tc("get_workspace", call_id="s1")]},
            {"content": "done", "tool_calls": []},
        ]
    )
    registry = _StubRegistry(payload_tools=["get_workspace"])
    _run_turn_with(sid, client, registry)
    step_ends = [e for e in _events_of(sid) if e.type == session_log.STEP_END]
    assert not any(e.data.get("parallel") for e in step_ends)


def test_usage_lands_on_step_and_turn_end_when_reported():
    sid = _mk_session("loop-usage")
    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "hi"})

    class _UsageClient(_ScriptedStreamClient):
        def chat_stream(self, messages, temperature=0.4, tools=None, tool_choice=None):
            reply = self._replies.pop(0)
            return self._stream(reply)

        async def _stream(self, reply):
            async for ev in super()._stream(reply):
                yield ev
            if reply.get("usage"):
                yield {"type": "usage", "usage": reply["usage"]}

    client = _UsageClient(
        [{"content": "quick", "tool_calls": [], "usage": {"prompt_tokens": 12, "completion_tokens": 7}}]
    )
    registry = _StubRegistry(payload_tools=["get_workspace"])
    _run_turn_with(sid, client, registry)
    events = _events_of(sid)
    step_end = next(e for e in events if e.type == session_log.STEP_END)
    assert step_end.data["usage"]["total_tokens"] == 19
    turn_end = next(e for e in events if e.type == session_log.TURN_END)
    assert turn_end.data["usage"]["total_tokens"] == 19


def test_usage_absent_when_server_reports_none():
    sid = _mk_session("loop-no-usage")
    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "hi"})
    client = _ScriptedStreamClient([{"content": "plain", "tool_calls": []}])
    registry = _StubRegistry(payload_tools=["get_workspace"])
    _run_turn_with(sid, client, registry)
    events = _events_of(sid)
    step_end = next(e for e in events if e.type == session_log.STEP_END)
    assert "usage" not in step_end.data
    turn_end = next(e for e in events if e.type == session_log.TURN_END)
    assert "usage" not in turn_end.data
