"""ask_user question cards: the tool result, the runner's real-seq stamping,
and the structured approval scan."""

from __future__ import annotations

import asyncio

import pytest

from calliope.agent.capabilities import catalog
from calliope.agent.harness import log as session_log
from calliope.agent.harness.runner import Runner
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
            "INSERT INTO agent_sessions (project_id, title, status) VALUES (NULL, 'ask', 'idle')"
        )
        session_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    return session_id


def test_ask_user_tool_result(sess):
    result = asyncio.run(
        catalog.execute(
            "ask_user",
            {"question": "Render now?", "options": ["Yes, render", "No"], "scope": "render"},
            None,
            sess,
        )
    )
    assert result["ok"] is True
    assert result["awaiting_input"] is True
    assert result["scope"] == "render"
    assert result["options"] == ["Yes, render", "No"]


def test_ask_user_defaults_and_validation(sess):
    # Fewer than 2 usable options → Yes/No defaults.
    result = asyncio.run(
        catalog.execute("ask_user", {"question": "Proceed?", "options": [""]}, None, sess)
    )
    assert result["options"] == ["Yes", "No"]
    assert asyncio.run(
        catalog.execute("ask_user", {"question": "", "options": ["a", "b"]}, None, sess)
    )["ok"] is False


def test_runner_stamps_real_seq_and_approval_scan(sess):
    runner = Runner()
    result = {
        "ok": True,
        "question": "Run it?",
        "options": ["Yes, run it", "No"],
        "scope": "shell",
        "question_seq": 0,
    }
    asyncio.run(runner._on_tool(sess, "ask_user", {"question": "Run it?"}, result))
    qseq = result["question_seq"]
    assert qseq > 0
    # Unanswered → nothing granted.
    assert session_log.has_structured_approval(sess, "shell") is False
    # The user clicks "Yes, run it": the endpoint appends question/answered.
    session_log.append_event(
        sess, "question/answered", {"question_seq": qseq, "answer": "Yes, run it"}
    )
    assert session_log.has_structured_approval(sess, "shell") is True
    # A refusal on a NEW card resets the verdict.
    q2 = session_log.append_event(
        sess, "question/asked", {"question": "Again?", "scope": "shell", "options": ["Yes", "No"]}
    )
    session_log.append_event(sess, "question/answered", {"question_seq": q2, "answer": "No"})
    assert session_log.has_structured_approval(sess, "shell") is False


def test_scope_info_never_grants(sess):
    runner = Runner()
    result = {
        "ok": True,
        "question": "Which ending?",
        "options": ["A", "B"],
        "scope": "info",
        "question_seq": 0,
    }
    asyncio.run(runner._on_tool(sess, "ask_user", {}, result))
    session_log.append_event(
        sess,
        "question/answered",
        {"question_seq": result["question_seq"], "answer": "A"},
    )
    # info never grants a render/shell/destructive approval.
    assert session_log.has_structured_approval(sess, "render") is False
    assert session_log.has_structured_approval(sess, "shell") is False
