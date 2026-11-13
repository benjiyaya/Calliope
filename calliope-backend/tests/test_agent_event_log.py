"""Event log invariants: append-only source of truth, derived reads, steering
watermark, structured approvals, and the usage/question-event stamps."""

from __future__ import annotations

import asyncio

import pytest

from calliope.agent.harness import log as session_log
from calliope.agent.harness.runner import Runner
from calliope.config import settings
from calliope.db import get_db, migrate_db


@pytest.fixture
def logdb(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "assets_dir", tmp_path / "assets")
    asyncio.run(migrate_db(settings.db_path))
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO agent_sessions (project_id, title, status) VALUES (NULL, 'log', 'idle')"
        )
        session_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    return session_id


def test_append_read_roundtrip(logdb):
    seq = session_log.append_event(logdb, "user/message", {"content": "hello"})
    seq2 = session_log.append_event(logdb, "assistant/message", {"content": "hi"})
    events = session_log.read_events(logdb)
    assert [e.seq for e in events] == [seq, seq2]
    assert events[0].data["content"] == "hello"
    assert events[1].type == "assistant/message"


def test_seq_allocation_is_monotonic(logdb):
    for i in range(5):
        session_log.append_event(logdb, "user/message", {"n": i})
    events = session_log.read_events(logdb)
    assert [e.seq for e in events] == [1, 2, 3, 4, 5]


def test_backfill_legacy_messages(logdb):
    conn = get_db(settings.db_path)
    try:
        conn.execute(
            "INSERT INTO agent_messages (session_id, role, content) VALUES (?, 'user', 'legacy question')",
            (logdb,),
        )
        conn.execute(
            "INSERT INTO agent_messages (session_id, role, content) VALUES (?, 'assistant', 'legacy answer')",
            (logdb,),
        )
        conn.commit()
    finally:
        conn.close()
    session_log.backfill_from_messages(logdb)
    rows = session_log.derive_chat_rows(session_log.read_events(logdb))
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[0]["content"] == "legacy question"
    # Idempotent: a session that already has events is not re-copied.
    session_log.append_event(logdb, "user/message", {"content": "new"})
    session_log.backfill_from_messages(logdb)
    rows = session_log.derive_chat_rows(session_log.read_events(logdb))
    assert rows[-1]["content"] == "new"


def test_derive_chat_rows_projects_steering_and_attachments(logdb):
    session_log.append_event(
        logdb,
        "user/message",
        {
            "content": "here",
            "mentions": [{"type": "workflow", "id": 3}],
            "attachments": [{"path": "/x.png", "kind": "image"}],
        },
    )
    session_log.append_event(
        logdb, "steering/message", {"content": "make it darker", "status": "steering"}
    )
    rows = session_log.derive_chat_rows(session_log.read_events(logdb))
    assert rows[0]["mentions"] == [{"type": "workflow", "id": 3}]
    assert rows[0]["attachments"][0]["kind"] == "image"
    assert rows[1]["status"] == "steering"
    assert rows[1]["role"] == "user"


def test_derive_plan_projects_task_statuses(logdb):
    session_log.append_event(
        logdb,
        "plan/created",
        {
            "tasks": [
                {"role": "script", "goal": "g", "status": "pending"},
                {"role": "render", "goal": "g", "status": "pending"},
            ]
        },
    )
    session_log.append_event(logdb, "task/start", {"index": 0, "status": "running", "role": "script"})
    session_log.append_event(logdb, "task/end", {"index": 0, "status": "verified", "role": "script"})
    session_log.append_event(logdb, "task/end", {"index": 1, "status": "blocked", "role": "render"})
    plan = session_log.derive_plan(session_log.read_events(logdb))
    assert plan is not None
    assert [task["status"] for task in plan["tasks"]] == ["verified", "blocked"]


def test_steering_after_watermark_advances_and_prevents_redrain(logdb):
    session_log.append_event(logdb, "user/message", {"content": "go"})
    session_log.append_event(logdb, "steering/message", {"content": "first note"})
    session_log.append_event(logdb, "assistant/message", {"content": "working"})
    session_log.append_event(logdb, "steering/message", {"content": "second note"})
    notes, watermark = session_log.steering_after(logdb, 0)
    assert notes == ["first note", "second note"]
    assert watermark == 4
    # Draining again from the advanced watermark yields nothing.
    notes2, watermark2 = session_log.steering_after(logdb, watermark)
    assert notes2 == [] and watermark2 == watermark


def test_has_structured_approval_flow(logdb):
    assert session_log.has_structured_approval(logdb, "shell") is False
    qseq = session_log.append_event(
        logdb, "question/asked", {"question": "run it?", "scope": "shell", "options": ["Yes, run it", "No"]}
    )
    # Unanswered card grants nothing.
    assert session_log.has_structured_approval(logdb, "shell") is False
    session_log.append_event(
        logdb, "question/answered", {"question_seq": qseq, "answer": "Yes, run it"}
    )
    assert session_log.has_structured_approval(logdb, "shell") is True
    # Refusal option does NOT grant.
    q2 = session_log.append_event(
        logdb, "question/asked", {"question": "again?", "scope": "shell", "options": ["Yes", "No"]}
    )
    session_log.append_event(logdb, "question/answered", {"question_seq": q2, "answer": "No"})
    assert session_log.has_structured_approval(logdb, "shell") is False


def test_has_structured_approval_scope_must_match(logdb):
    qseq = session_log.append_event(
        logdb, "question/asked", {"question": "replace?", "scope": "destructive_replace"}
    )
    session_log.append_event(
        logdb, "question/answered", {"question_seq": qseq, "answer": "yes"}
    )
    assert session_log.has_structured_approval(logdb, "destructive_replace") is True
    assert session_log.has_structured_approval(logdb, "shell") is False


def test_runner_stamps_real_question_seq(logdb):
    """The ask_user tool result carries the REAL event seq — the UI's
    answer_to and the approval scan both match on it."""

    async def scenario():
        runner = Runner()
        result = {
            "ok": True,
            "question": "proceed?",
            "options": ["Yes", "No"],
            "scope": "shell",
            "question_seq": 0,
        }
        await runner._on_tool(logdb, "ask_user", {"question": "proceed?"}, result)
        return result

    result = asyncio.run(scenario())
    events = session_log.read_events(logdb)
    asked = [e for e in events if e.type == "question/asked"]
    assert len(asked) == 1
    assert result["question_seq"] == asked[0].seq
    assert result["question_seq"] > 0
    tool_results = [e for e in events if e.type == "tool/result"]
    assert tool_results[0].data["tool_result"]["question_seq"] == asked[0].seq


def test_usage_lands_on_assistant_message_event(logdb):
    runner = Runner()
    asyncio.run(
        runner._store_assistant(
            logdb,
            "done",
            agent_name=None,
            usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        )
    )
    events = session_log.read_events(logdb)
    final = [e for e in events if e.type == "assistant/message"][-1]
    assert final.data["usage"]["total_tokens"] == 120


def test_usage_absent_when_not_reported(logdb):
    runner = Runner()
    asyncio.run(runner._store_assistant(logdb, "done", agent_name=None))
    events = session_log.read_events(logdb)
    final = [e for e in events if e.type == "assistant/message"][-1]
    assert "usage" not in final.data
