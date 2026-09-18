"""Mid-run steering — the user course-corrects a running turn.

Contract pieces under test:
- runner.steer(): appends steering/message + mirrors a status='steering'
  chat row while a turn is running; refuses (None) when idle.
- router POST /messages: 409 falls back to steering instead of rejecting;
  answer_to while running stays a 409 (answers belong to the next turn).
- derive_llm_history: steering projects as [STEERING] user content, buffers
  mid-tool-exchange (never inside a tool_calls/result pair), and never opens
  a user-turn boundary or affects policy's latest_user_message.
- run_turn / _run_sub_agent: drained steering text reaches the model at the
  next step boundary.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from calliope.agent.harness.registry import ToolContext
from calliope.config import settings
from calliope.db import get_db


@pytest.fixture(autouse=True, scope="module")
def _never_touch_real_db():
    prev = settings.data_dir
    prev_assets = settings.assets_dir
    with tempfile.TemporaryDirectory() as tmp:
        settings.data_dir = Path(tmp)
        settings.assets_dir = Path(tmp) / "assets"
        asyncio.run(_migrate(settings.db_path))
        yield
    settings.data_dir = prev
    settings.assets_dir = prev_assets


async def _migrate(db_path):
    from calliope.db import migrate_db

    await migrate_db(db_path)


def _mk_session(title: str = "steer-test") -> int:
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO agent_sessions (title, project_id) VALUES (?, NULL)", (title,)
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


class _FakeTask:
    def done(self) -> bool:
        return False


def _fake_running(sid: int):
    from calliope.agent.harness.runner import runner

    runner._tasks[sid] = _FakeTask()  # type: ignore[assignment]
    return runner


def _events(sid: int):
    from calliope.agent.harness import log as session_log

    return session_log.read_events(sid)


# ── projection: derive_llm_history ──────────────────────────────────────


def test_steering_projects_as_user_message_with_header():
    from calliope.agent.harness import log as session_log

    sid = _mk_session()
    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "build the scene"})
    session_log.append_event(sid, session_log.STEERING_MESSAGE, {"content": "make it darker"})
    history = session_log.derive_llm_history(_events(sid))
    steer = [m for m in history if session_log.STEERING_INJECT_HEADER in str(m.get("content"))]
    assert steer, "steering never projected"
    assert steer[0]["role"] == "user"
    assert "make it darker" in str(steer[0]["content"])


def test_steering_never_lands_inside_tool_exchange():
    from calliope.agent.harness import log as session_log

    sid = _mk_session()
    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "go"})
    session_log.append_event(
        sid,
        session_log.ASSISTANT_MESSAGE,
        {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
    )
    # Steering lands AFTER the call, BEFORE its result — must buffer.
    session_log.append_event(sid, session_log.STEERING_MESSAGE, {"content": "wait, no"})
    session_log.append_event(
        sid, session_log.TOOL_RESULT, {"call_id": "c1", "tool_name": "t", "result": {"ok": True}}
    )
    history = session_log.derive_llm_history(_events(sid))
    roles = [m["role"] for m in history]
    # No user message between assistant(tool_calls) and its tool result.
    i_asst = next(i for i, m in enumerate(history) if m.get("tool_calls"))
    assert history[i_asst + 1]["role"] == "tool"
    # And it flushed right after the exchange closed.
    assert any(session_log.STEERING_INJECT_HEADER in str(m.get("content")) for m in history)


def test_steering_does_not_open_user_turn_boundary():
    from calliope.agent.harness import log as session_log

    sid = _mk_session()
    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "first turn"})
    session_log.append_event(sid, session_log.STEERING_MESSAGE, {"content": "adjust"})
    # max_user_turns=1 keeps exactly the last user turn; steering before it
    # belongs to that same turn and must survive (not be dropped as its own).
    history = session_log.derive_llm_history(_events(sid), max_user_turns=1)
    assert any("adjust" in str(m.get("content")) for m in history)
    assert sum(1 for m in history if m["role"] == "user") == 2  # the turn message + its steering


def test_steering_not_visible_to_policy_latest_user_message():
    from calliope.agent.harness import log as session_log

    sid = _mk_session()
    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "plain text only"})
    session_log.append_event(sid, session_log.STEERING_MESSAGE, {"content": "yes generate everything"})
    assert session_log.latest_user_message(sid) == "plain text only"


def test_chat_rows_include_steering_status():
    from calliope.agent.harness import log as session_log

    sid = _mk_session()
    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "hi"})
    session_log.append_event(sid, session_log.STEERING_MESSAGE, {"content": "actually no"})
    rows = session_log.derive_chat_rows(_events(sid))
    steer_rows = [r for r in rows if r.get("status") == "steering"]
    assert steer_rows and steer_rows[0]["role"] == "user" and steer_rows[0]["content"] == "actually no"


def test_drain_watermark_prevents_redrain():
    from calliope.agent.harness import log as session_log

    sid = _mk_session()
    e1 = session_log.append_event(sid, session_log.STEERING_MESSAGE, {"content": "one"})
    e2 = session_log.append_event(sid, session_log.STEERING_MESSAGE, {"content": "two"})
    first = session_log.drain_steering(sid, 0)
    assert [s.data["content"] for s in first] == ["one", "two"]
    assert first[0].seq == e1.seq and first[1].seq == e2.seq
    # Already drained past both — nothing new.
    assert session_log.drain_steering(sid, e2.seq) == []


# ── runner.steer ────────────────────────────────────────────────────────


def test_runner_steer_appends_event_and_row_when_running():
    from calliope.agent.harness.runner import runner

    sid = _mk_session()
    try:
        _fake_running(sid)
        row = asyncio.run(runner.steer(sid, "make it moodier"))
    finally:
        runner._tasks.pop(sid, None)

    assert row is not None and row["status"] == "steering" and row["role"] == "user"
    types = [e.type for e in _events(sid)]
    assert "steering/message" in types
    # No turn/start, no user/message — steering never starts a turn.
    assert "turn/start" not in types
    assert "user/message" not in types


def test_runner_steer_refuses_when_idle():
    from calliope.agent.harness.runner import runner

    sid = _mk_session()
    assert asyncio.run(runner.steer(sid, "hello")) is None
    assert all(e.type != "steering/message" for e in _events(sid))


# ── router fallback ─────────────────────────────────────────────────────


def test_post_message_while_running_steers_instead_of_409(client):
    from calliope.agent.harness.runner import runner

    r = client.post("/api/agent/sessions", json={})
    sid = r.json()["id"]
    try:
        _fake_running(sid)
        resp = client.post(
            f"/api/agent/sessions/{sid}/messages", json={"content": "switch to wide shot"}
        )
    finally:
        runner._tasks.pop(sid, None)
    assert resp.status_code == 200
    body = resp.json()
    assert body["steered"] is True
    assert body["message"]["status"] == "steering"


def test_post_message_answer_to_while_running_stays_409(client):
    from calliope.agent.harness.runner import runner

    r = client.post("/api/agent/sessions", json={})
    sid = r.json()["id"]
    try:
        _fake_running(sid)
        resp = client.post(
            f"/api/agent/sessions/{sid}/messages",
            json={"content": "yes", "answer_to": 3},
        )
    finally:
        runner._tasks.pop(sid, None)
    assert resp.status_code == 409


# ── loop injection ──────────────────────────────────────────────────────


class _SteeringAwareClient:
    """chat_stream that records requests; steers once after the first step."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests: list[list[dict]] = []

    def chat_stream(self, messages, temperature=0.4, tools=None):
        self.requests.append(list(messages))
        reply = self.replies.pop(0)
        return _StreamReply(reply)

    async def close(self):
        pass


class _StreamReply:
    def __init__(self, chunks):
        self._chunks = [c for c in chunks if c]

    def __aiter__(self):
        self._iter = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


def _tc(name: str, call_id: str):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def test_run_turn_drains_steering_at_step_boundary():
    import calliope.agent.harness.loop as loop_mod
    from calliope.agent.harness import log as session_log

    sid = _mk_session()
    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "frame the shot"})

    client_llm = _SteeringAwareClient(
        [
            [{"type": "tool_call", "tool_call": _tc("get_scene", "c1")}],
            [{"type": "delta", "content": "done"}],
        ]
    )

    class _Reg:
        async def execute(self, ctx, name, args):
            # Steering lands mid-step (tool executing) — the drain happens at
            # the NEXT step boundary, after this result is appended.
            session_log.append_event(
                sid, session_log.STEERING_MESSAGE, {"content": "use a low angle instead"}
            )
            return {"ok": True}

        def openai_payload(self, ctx):
            return []

    async def _run():
        orig_llm = loop_mod.LLMClient
        orig_reg = loop_mod.get_registry
        orig_prompts = loop_mod.get_prompts
        loop_mod.LLMClient = lambda: client_llm
        loop_mod.get_registry = lambda: _Reg()

        class _P:
            async def assemble(self, ctx):
                return "system"

        loop_mod.get_prompts = lambda: _P()
        try:
            return await loop_mod.run_turn(
                ToolContext(session_id=sid, project_id=None), []
            )
        finally:
            loop_mod.LLMClient = orig_llm
            loop_mod.get_registry = orig_reg
            loop_mod.get_prompts = orig_prompts

    final = asyncio.run(_run())
    assert final == "done"
    # Second request (step 2) must contain the steering text, positioned
    # after the tool result (never between tool_calls and its result).
    second = client_llm.requests[1]
    texts = [str(m.get("content")) for m in second]
    assert any("use a low angle instead" in t for t in texts)
    # Find position: must come after the tool message.
    tool_idx = next(i for i, m in enumerate(second) if m.get("role") == "tool")
    steer_idx = next(i for i, t in enumerate(texts) if "use a low angle" in t)
    assert steer_idx > tool_idx


def test_sub_agent_loop_drains_steering():
    import calliope.agent.harness.orchestrator as orch
    from calliope.agent.harness import log as session_log

    sid = _mk_session()

    class _Scripted:
        def __init__(self):
            self.seen: list[list[dict]] = []

        async def chat_with_tools(self, messages, temperature=0.7, tools=None, tool_choice=None):
            self.seen.append(list(messages))
            if len(self.seen) == 1:
                return {"role": "assistant", "content": None, "tool_calls": [_tc("get_workspace", "a")]}
            return {"role": "assistant", "content": "adjusted", "tool_calls": []}

        async def close(self):
            pass

    scripted = _Scripted()

    class _Reg:
        async def execute(self, ctx, name, args):
            session_log.append_event(
                sid, session_log.STEERING_MESSAGE, {"content": "skip the wide shots"}
            )
            return {"ok": True}

        def get(self, name):
            return None

        def _visible(self, t, ctx):
            return True

    async def _run():
        orig_c, orig_r = orch.LLMClient, orch.get_registry
        orch.LLMClient = lambda: scripted
        orch.get_registry = lambda: _Reg()
        try:
            return await orch._run_sub_agent(
                ToolContext(session_id=sid, project_id=1),
                [{"role": "user", "content": "do"}],
                ["get_workspace"],
                agent_name="script-agent",
                max_iterations=4,
            )
        finally:
            orch.LLMClient = orig_c
            orch.get_registry = orig_r

    final = asyncio.run(_run())
    assert final == "adjusted"
    second = scripted.seen[1]
    assert any("skip the wide shots" in str(m.get("content")) for m in second)
