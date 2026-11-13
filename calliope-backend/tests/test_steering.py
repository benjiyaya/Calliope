"""Mid-run steering: course-correct without killing the run.

A message sent while a turn runs becomes a steering/message event (never
user/message), drained at step boundaries and injected as [STEERING] notes.
Question-card answers (answer_to) stay 409 while running — the answer
belongs to the next turn.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from calliope.agent.llm import ChatTurn, ToolCall
from calliope.agent.harness.runner import Runner
from calliope.agent.runtime import subagent as subagent_mod
from calliope.agent.runtime.subagent import run_subagent
from calliope.config import settings
from calliope.db import get_db, migrate_db


@pytest.fixture
def sess(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "assets_dir", tmp_path / "assets")
    asyncio.run(migrate_db(settings.db_path))
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO agent_sessions (project_id, title, status) VALUES (NULL, 'steer', 'idle')"
        )
        session_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    return session_id


class Scripted:
    """Scripted LLM client; captures messages so tests can assert the
    [STEERING] injections land between steps."""

    def __init__(self, turns: list[ChatTurn]) -> None:
        self.turns = turns
        self.seen: list[list[dict]] = []

    def __call__(self, *_args, **_kwargs):
        return self

    async def chat_with_tools(
        self, messages, temperature=0.7, tools=None, tool_choice=None,
        on_token=None, on_reasoning=None, on_usage=None,
    ):
        self.seen.append(list(messages))
        if not self.turns:
            raise AssertionError("scripted model ran out of replies")
        turn = self.turns.pop(0)
        return {
            "role": "assistant",
            "content": turn.content or "",
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in turn.tool_calls
            ],
        }

    async def close(self) -> None:
        return None


def test_steer_appends_event_and_row_when_running(sess):
    runner = Runner()
    runner._running.add(sess)
    runner._steer_seq[sess] = 0
    message = asyncio.run(runner.steer(sess, "make it darker"))
    assert message is not None
    from calliope.agent.harness import log as session_log

    events = session_log.read_events(sess)
    assert events[-1].type == "steering/message"
    assert events[-1].data["content"] == "make it darker"
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT status FROM agent_messages WHERE id = ?", (message["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "steering"


def test_steer_refuses_when_idle(sess):
    runner = Runner()
    assert asyncio.run(runner.steer(sess, "hello")) is None


def test_runner_drains_steering_between_steps(sess):
    """pull_steering drains from the user/message watermark — the turn's own
    user message is never re-drained as steering."""
    runner = Runner()
    runner._running.add(sess)

    async def scenario():
        seq = session_log_append(runner, sess)
        runner._steer_seq[sess] = seq
        first = runner._drain_steering(sess)
        await runner.steer(sess, "stop at the valley")
        await runner.steer(sess, "keep the fog")
        second = runner._drain_steering(sess)
        third = runner._drain_steering(sess)
        return first, second, third

    first, second, third = asyncio.run(scenario())
    assert first == []
    assert second == ["stop at the valley", "keep the fog"]
    # Watermark advanced — no double-drain.
    assert third == []


def session_log_append(runner, sess):
    from calliope.agent.harness import log as session_log

    return session_log.append_event(sess, "user/message", {"content": "go"})


def test_subagent_injects_steering_between_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "assets_dir", tmp_path / "assets")
    asyncio.run(migrate_db(settings.db_path))

    drained: list[list[str]] = []

    def pull():
        notes = ["tighten the shot"]
        drained.append(notes)
        return notes

    client = Scripted(
        [
            ChatTurn(content=None, tool_calls=[ToolCall(id="c1", name="list_memories", arguments={})]),
            ChatTurn(content="done"),
        ]
    )
    monkeypatch.setattr(subagent_mod, "LLMClient", client)

    async def scenario():
        return await run_subagent(
            project_id=None,
            session_id=None,
            grant="script",
            goal="work",
            pull_steering=pull,
        )

    result = asyncio.run(scenario())
    assert result.finished is True
    # The steering note was injected as a user message BEFORE the second step.
    second_step_messages = client.seen[1]
    injected = [
        m for m in second_step_messages if m.get("role") == "user" and "[STEERING]" in str(m.get("content"))
    ]
    assert any("tighten the shot" in str(m["content"]) for m in injected)


def test_post_message_while_running_steers_instead_of_409(client):
    from calliope.agent.harness import log as session_log
    from calliope.agent.harness.runner import runner as global_runner

    sid = client.post("/api/agent/sessions", json={"title": "steer"}).json()["id"]
    # Simulate a running turn.
    global_runner._running.add(sid)

    try:
        resp = client.post(f"/api/agent/sessions/{sid}/messages", json={"content": "wait, darker"})
        assert resp.status_code == 200
        assert resp.json()["steered"] is True
        conn = get_db(settings.db_path)
        try:
            row = conn.execute(
                "SELECT status FROM agent_messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (sid,),
            ).fetchone()
        finally:
            conn.close()
        assert row["status"] == "steering"
        events = session_log.read_events(sid)
        assert events[-1].type == "steering/message"
    finally:
        global_runner._running.discard(sid)


def test_answer_to_while_running_stays_409(client):
    from calliope.agent.harness.runner import runner as global_runner

    sid = client.post("/api/agent/sessions", json={"title": "steer"}).json()["id"]
    global_runner._running.add(sid)
    try:
        resp = client.post(
            f"/api/agent/sessions/{sid}/messages",
            json={"content": "Yes", "answer_to": 7},
        )
        assert resp.status_code == 409
    finally:
        global_runner._running.discard(sid)
