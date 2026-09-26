"""Golden-scenario replay harness.

Replays recorded agent_events (from fixtures — synthetic, never real user
sessions) through run_turn with the LLM mocked to REPLAY the recorded
assistant outputs, then asserts structural invariants on the NEW event log:

- every assistant tool_calls message has a matching tool result (no dangling
  call ids in derive_llm_history),
- turn/end exists with a valid status,
- bridged/parallel paths produce the same derived shape as sequential.

The replay LLM is driven by the fixture itself: each fixture embeds an
ordered list of assistant replies (the same shape run_turn's stream yields),
so a fixture both documents a golden scenario and deterministically
re-executes it.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from calliope.agent.harness import loop as loop_mod
from calliope.agent.harness import log as session_log
from calliope.agent.harness.registry import ToolContext
from calliope.config import settings
from calliope.db import get_db

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "replay"

VALID_TURN_STATUSES = {"completed", "step_budget_exhausted", "cancelled", "failed", "awaiting_input"}


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


def _mk_session(title: str) -> int:
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO agent_sessions (title, project_id) VALUES (?, NULL)", (title,)
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _load_fixtures() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(FIXTURES_DIR.glob("*.json"))]


class _ReplayClient:
    """One scripted reply per step, popped in order; records usage seen."""

    def __init__(self, replies: list[dict]):
        self._replies = list(replies)
        self.closed = False

    def chat_stream(self, messages, temperature=0.4, tools=None, tool_choice=None):
        reply = self._replies.pop(0) if self._replies else {
            "role": "assistant", "content": "done", "tool_calls": []
        }
        return self._stream(reply)

    async def _stream(self, reply: dict):
        if reply.get("content"):
            yield {"type": "delta", "content": reply["content"]}
        for tc in reply.get("tool_calls") or []:
            yield {"type": "tool_call", "tool_call": tc}
        if reply.get("usage"):
            yield {"type": "usage", "usage": reply["usage"]}
        yield {"type": "done"}

    async def close(self):
        self.closed = True


class _RecordingRegistry:
    """Executes only the fixture's expected tools; records (name, args)."""

    def __init__(
        self,
        results_by_tool: dict[str, dict] | None = None,
        payload_tools: list[str] | None = None,
    ):
        self.executed: list[tuple[str, dict]] = []
        self.results_by_tool = results_by_tool or {}
        self.payload_tools = payload_tools or []

    def openai_payload(self, ctx):
        return [
            {"type": "function", "function": {"name": n, "description": "", "parameters": {}}}
            for n in self.payload_tools
        ]

    def get(self, name):
        if name in self.payload_tools or name in self.results_by_tool:
            return {"name": name}
        return None

    async def execute(self, ctx, name, args):
        self.executed.append((name, dict(args)))
        r = self.results_by_tool.get(name)
        if r is not None:
            return dict(r)
        if name == "ask_user":
            return {
                "ok": True,
                "awaiting_user_input": True,
                "question": "Render scene 1 now?",
            }
        return {"ok": True}


def _replay(fixture: dict, registry: _RecordingRegistry) -> list[dict]:
    """Load the fixture's events into a fresh session, replay its assistant
    replies through run_turn, return the NEW log's derived history."""
    sid = _mk_session(f"replay-{fixture['name']}")
    for e in fixture["events"]:
        session_log.append_event(sid, e["type"], dict(e["data"]))

    # The replay replies are the fixture's assistant messages (in order).
    replies = [
        {"content": e["data"].get("content"), "tool_calls": e["data"].get("tool_calls") or [], "usage": e["data"].get("usage")}
        for e in fixture["events"]
        if e["type"] == "assistant/message"
    ]
    client = _ReplayClient(replies)
    orig_client = loop_mod.LLMClient
    orig_registry = loop_mod.get_registry
    loop_mod.LLMClient = lambda *a, **k: client
    loop_mod.get_registry = lambda: registry
    try:
        ctx = ToolContext(session_id=sid, project_id=None)
        history = session_log.derive_llm_history(session_log.read_events(sid))
        asyncio.run(loop_mod.run_turn(ctx, history))
    finally:
        loop_mod.LLMClient = orig_client
        loop_mod.get_registry = orig_registry
    return session_log.derive_llm_history(session_log.read_events(sid))


def _assert_common_invariants(fixture: dict, derived: list[dict]) -> None:
    """Invariants shared by every golden scenario."""
    # 1. Every assistant tool_calls message has matching tool results — no
    #    dangling call ids (derive would otherwise 400 on strict servers).
    call_ids: list[str] = []
    for m in derived:
        for tc in m.get("tool_calls") or []:
            call_ids.append(tc["id"])
    result_ids = {m["tool_call_id"] for m in derived if m.get("role") == "tool"}
    assert call_ids, f"{fixture['name']}: replay produced no tool calls"
    for cid in call_ids:
        assert cid in result_ids, f"{fixture['name']}: dangling call id {cid}"

    # 2. turn/end exists with a valid status.
    events = session_log.read_events(_session_id_for(fixture))
    turn_ends = [e for e in events if e.type == session_log.TURN_END]
    assert turn_ends, f"{fixture['name']}: no turn/end event"
    status = turn_ends[-1].data.get("status")
    assert status in VALID_TURN_STATUSES, f"{fixture['name']}: invalid turn status {status}"

    # 3. step/end present for every step/start.
    starts = sum(1 for e in events if e.type == session_log.STEP_START)
    ends = sum(1 for e in events if e.type == session_log.STEP_END)
    assert starts == ends, f"{fixture['name']}: {starts} step/start vs {ends} step/end"


_SESSION_IDS: dict[str, int] = {}


def _session_id_for(fixture: dict) -> int:
    return _SESSION_IDS[fixture["name"]]


@pytest.mark.parametrize("fixture", _load_fixtures(), ids=lambda f: f["name"])
def test_replay_invariants(fixture: dict):
    registry = _RecordingRegistry()
    derived = _replay(fixture, registry)
    _SESSION_IDS[fixture["name"]] = _last_session_id()
    _assert_common_invariants(fixture, derived)


def _last_session_id() -> int:
    conn = get_db(settings.db_path)
    try:
        row = conn.execute("SELECT MAX(id) AS m FROM agent_sessions").fetchone()
        return int(row["m"])
    finally:
        conn.close()


def test_bridge_replay_matches_sequential_shape():
    """A bridged JSON-action reply must produce the SAME derived history shape
    as a native tool-call reply (assistant tool_calls -> tool result), so
    downstream history replay cannot tell the paths apart."""
    sid = _mk_session("replay-bridge")
    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "get workspace"})

    bridge_reply = {
        "content": '```json\n{"tool": "get_workspace", "arguments": {}}\n```',
        "tool_calls": [],
    }
    final_reply = {"content": "All done.", "tool_calls": []}
    client = _ReplayClient([bridge_reply, final_reply])
    registry = _RecordingRegistry(
        {"get_workspace": {"ok": True, "characters": 0}}, payload_tools=["get_workspace"]
    )

    orig_client, orig_registry = loop_mod.LLMClient, loop_mod.get_registry
    loop_mod.LLMClient = lambda *a, **k: client
    loop_mod.get_registry = lambda: registry
    try:
        ctx = ToolContext(session_id=sid, project_id=None)
        history = session_log.derive_llm_history(session_log.read_events(sid))
        asyncio.run(loop_mod.run_turn(ctx, history))
    finally:
        loop_mod.LLMClient, loop_mod.get_registry = orig_client, orig_registry

    derived = session_log.derive_llm_history(session_log.read_events(sid))
    bridge_assistant = [m for m in derived if m.get("tool_calls")]
    assert bridge_assistant, "bridged call did not materialize as assistant tool_calls"
    assert bridge_assistant[0]["tool_calls"][0]["id"].startswith("bridge_")
    result_msgs = [m for m in derived if m.get("role") == "tool"]
    assert result_msgs and result_msgs[0]["tool_call_id"].startswith("bridge_")
    # The registry executed exactly once through the same guarded path.
    assert [n for n, _ in registry.executed] == ["get_workspace"]


def test_parallel_replay_preserves_order():
    """A read-only parallel batch (all allowlisted) keeps the batch's original
    call order in the derived history — gather must not reorder results."""
    sid = _mk_session("replay-parallel")
    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "inspect"})

    batch_reply = {
        "content": None,
        "tool_calls": [
            {"id": "p1", "type": "function", "function": {"name": "get_workspace", "arguments": "{}"}},
            {"id": "p2", "type": "function", "function": {"name": "list_workflows", "arguments": "{}"}},
            {"id": "p3", "type": "function", "function": {"name": "list_memories", "arguments": "{}"}},
        ],
    }
    final_reply = {"content": "done", "tool_calls": []}
    client = _ReplayClient([batch_reply, final_reply])
    registry = _RecordingRegistry()

    orig_client, orig_registry = loop_mod.LLMClient, loop_mod.get_registry
    loop_mod.LLMClient = lambda *a, **k: client
    loop_mod.get_registry = lambda: registry
    try:
        ctx = ToolContext(session_id=sid, project_id=None)
        history = session_log.derive_llm_history(session_log.read_events(sid))
        asyncio.run(loop_mod.run_turn(ctx, history))
    finally:
        loop_mod.LLMClient, loop_mod.get_registry = orig_client, orig_registry

    derived = session_log.derive_llm_history(session_log.read_events(sid))
    results = [m["tool_call_id"] for m in derived if m.get("role") == "tool"]
    assert results == ["p1", "p2", "p3"], f"order drifted: {results}"
    events = session_log.read_events(sid)
    step_ends = [e for e in events if e.type == session_log.STEP_END]
    assert step_ends and step_ends[0].data.get("parallel") is True


def test_usage_recorded_when_server_reports_it():
    """step/end + turn/end carry usage only when the server reported it."""
    sid = _mk_session("replay-usage")
    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "hi"})

    usage_reply = {
        "content": "quick answer",
        "tool_calls": [],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    client = _ReplayClient([usage_reply])
    orig_client, orig_registry = loop_mod.LLMClient, loop_mod.get_registry
    loop_mod.LLMClient = lambda *a, **k: client
    loop_mod.get_registry = lambda: _RecordingRegistry()
    try:
        ctx = ToolContext(session_id=sid, project_id=None)
        history = session_log.derive_llm_history(session_log.read_events(sid))
        asyncio.run(loop_mod.run_turn(ctx, history))
    finally:
        loop_mod.LLMClient, loop_mod.get_registry = orig_client, orig_registry

    events = session_log.read_events(sid)
    step_end = next(e for e in events if e.type == session_log.STEP_END)
    assert step_end.data.get("usage") == {
        "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15
    }
    turn_end = next(e for e in events if e.type == session_log.TURN_END)
    assert turn_end.data.get("usage", {}).get("total_tokens") == 15
