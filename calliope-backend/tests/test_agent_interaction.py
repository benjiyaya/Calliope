"""Tests for the interaction plugin (ask_user) + structured approvals."""
from __future__ import annotations

import pytest

from calliope.agent.harness import log as session_log
from calliope.agent.harness import policy
from calliope.agent.harness.plugins import interaction
from calliope.agent.harness.registry import ToolContext
from calliope.agent.harness.tools import execute_tool


@pytest.fixture
def session(client):
    resp = client.post("/api/agent/sessions", json={})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _ctx(session, project_id=None):
    return ToolContext(session_id=session["id"], project_id=project_id)


def test_ask_user_appends_question_event(session):
    result = execute_tool(
        _ctx(session),
        "ask_user",
        {"question": "Generate images for all 3 characters?", "options": ["Yes, generate", "No, not yet"], "scope": "render"},
    )
    import asyncio

    result = asyncio.run(result) if hasattr(result, "__await__") else result
    assert result["ok"] is True
    assert result["awaiting_user_input"] is True
    events = session_log.read_events(session["id"])
    asked = [e for e in events if e.type == session_log.QUESTION_ASKED]
    assert len(asked) == 1
    assert asked[0].data["scope"] == "render"
    assert asked[0].data["options"] == ["Yes, generate", "No, not yet"]


def test_ask_user_validates_input(session):
    import asyncio

    ctx = _ctx(session)
    too_few = asyncio.run(execute_tool(ctx, "ask_user", {"question": "q", "options": ["yes"]}))
    assert too_few["ok"] is False
    empty = asyncio.run(execute_tool(ctx, "ask_user", {"question": "", "options": ["a", "b"]}))
    assert empty["ok"] is False
    bad_scope = asyncio.run(
        execute_tool(ctx, "ask_user", {"question": "q", "options": ["a", "b"], "scope": "bogus"})
    )
    assert bad_scope["ok"] is False


def test_structured_approval_grants_render(session):
    import asyncio

    ctx = _ctx(session)
    asked = asyncio.run(
        execute_tool(ctx, "ask_user", {"question": "Render?", "options": ["Yes", "No"], "scope": "render"})
    )
    session_log.append_event(
        session["id"],
        session_log.QUESTION_ANSWERED,
        {"question_seq": asked["question_seq"], "answer": "Yes", "scope": "render"},
    )
    session_log.append_event(session["id"], session_log.USER_MESSAGE, {"content": "Yes"})
    assert policy.has_structured_approval(ctx, "render") is True
    assert policy.user_allows_render(ctx) is True
    # scope isolation: a render approval is not a destructive approval
    assert policy.has_structured_approval(ctx, "destructive_replace") is False


def test_structured_approval_negative_answer_grants_nothing(session):
    import asyncio

    ctx = _ctx(session)
    asked = asyncio.run(
        execute_tool(ctx, "ask_user", {"question": "Render?", "options": ["Yes", "No"], "scope": "render"})
    )
    session_log.append_event(
        session["id"],
        session_log.QUESTION_ANSWERED,
        {"question_seq": asked["question_seq"], "answer": "No", "scope": "render"},
    )
    session_log.append_event(session["id"], session_log.USER_MESSAGE, {"content": "No"})
    assert policy.has_structured_approval(ctx, "render") is False
    assert policy.user_allows_render(ctx) is False


def test_scope_info_never_grants(session):
    import asyncio

    ctx = _ctx(session)
    asked = asyncio.run(
        execute_tool(ctx, "ask_user", {"question": "Continue?", "options": ["Yes", "No"], "scope": "info"})
    )
    session_log.append_event(
        session["id"],
        session_log.QUESTION_ANSWERED,
        {"question_seq": asked["question_seq"], "answer": "Yes", "scope": "info"},
    )
    session_log.append_event(session["id"], session_log.USER_MESSAGE, {"content": "Yes"})
    assert policy.has_structured_approval(ctx, "info") is False
    assert policy.has_structured_approval(ctx, "render") is False


def test_open_question_is_visible_for_card_ui(session):
    import asyncio

    ctx = _ctx(session)
    asked = asyncio.run(
        execute_tool(ctx, "ask_user", {"question": "Which style?", "options": ["Painterly", "Anime"]})
    )
    open_q = interaction.latest_open_question(session["id"])
    assert open_q is not None
    assert open_q["seq"] == asked["question_seq"]
    assert open_q["options"] == ["Painterly", "Anime"]

    session_log.append_event(
        session["id"],
        session_log.QUESTION_ANSWERED,
        {"question_seq": asked["question_seq"], "answer": "Anime", "scope": "info"},
    )
    assert interaction.latest_open_question(session["id"]) is None


def test_denied_tool_result_carries_reason_code(client, session, monkeypatch):
    """Task 3: guard refusals expose a stable machine token."""
    import asyncio

    from calliope.agent.harness import get_registry

    project = client.post("/api/projects", json={"title": "Guard Codes"}).json()
    ctx = ToolContext(session_id=session["id"], project_id=project["id"])
    # A non-empty project + replace on a destructive tool → destructive guard
    registry = get_registry()
    tool = registry.get("generate_story")
    assert tool is not None and tool.destructive

    client.post(f"/api/projects/{project['id']}/characters", json={"name": "Someone"})
    result = asyncio.run(
        registry.execute(ctx, "generate_story", {"replace": True})
    )
    assert result["ok"] is False
    assert result.get("reason_code") == "guard_destructive_replace"


def test_answer_to_message_records_structured_approval(client, session):
    """The runner records question/answered before the user/message echo, so
    policy.latest_answer's adjacency rule sees a valid card answer."""
    import asyncio

    from calliope.agent.harness import policy

    ctx = ToolContext(session_id=session["id"], project_id=None)
    asked = asyncio.run(
        execute_tool(ctx, "ask_user", {"question": "Render now?", "options": ["Yes", "No"], "scope": "render"})
    )

    # MessageCreate.answer_to flows through the API the way the card click does.
    # The turn starts a real run, which needs an LLM — the message is persisted
    # regardless; the approval event is what we assert on.
    resp = client.post(
        f"/api/agent/sessions/{session['id']}/messages",
        json={"content": "Yes", "answer_to": asked["question_seq"]},
    )
    # The run may fail fast without an LLM configured; the API contract only
    # requires the message to be accepted (200) — the answer event must exist.
    assert resp.status_code == 200, resp.text
    assert policy.has_structured_approval(ctx, "render") is True
    assert policy.user_allows_render(ctx) is True


def test_card_option_sentence_grants_approval():
    """Options are full sentences ("Yes, replace with ~40 shorter scenes");
    the first word decides. Exact 'yes' still grants; 'No, …' refuses."""
    from calliope.agent.harness.policy import _answer_is_affirmative

    assert _answer_is_affirmative("Yes, replace with ~40 shorter scenes") is True
    assert _answer_is_affirmative("yes go!") is True
    assert _answer_is_affirmative("Sure, do it") is True
    assert _answer_is_affirmative("Go ahead") is True
    assert _answer_is_affirmative("No, keep existing scenes and add more at the end") is False
    assert _answer_is_affirmative("Just update durations, don't change content") is False
    assert _answer_is_affirmative("no") is False
    assert _answer_is_affirmative("") is False


def test_ask_user_logs_tool_result_before_turn_end(client, session):
    """The card derives from the persisted ask_user tool ROW, which needs the
    tool/result event — the loop must log + emit the result BEFORE ending the
    turn, or the UI shows a stuck 'working…' with no question (the reported
    bug: user had to Stop/refresh to see anything)."""
    import asyncio
    import json

    from calliope.agent.harness.loop import run_turn

    class _AskStream:
        def __aiter__(self):
            async def gen():
                yield {
                    "type": "tool_call",
                    "tool_call": {
                        "id": "c1",
                        "function": {
                            "name": "ask_user",
                            "arguments": json.dumps(
                                {
                                    "question": "Regenerate the script?",
                                    "options": ["Yes, replace", "No, keep"],
                                    "scope": "destructive_replace",
                                }
                            ),
                        },
                    },
                }

            return gen()

    class _Client:
        def chat_stream(self, messages, temperature=0.4, tools=None):
            return _AskStream()

        async def close(self):
            return None

    from calliope.agent.harness import loop as loop_mod

    orig_llm = loop_mod._llm_for_role
    loop_mod._llm_for_role = lambda role: _Client()
    ctx = ToolContext(session_id=session["id"], project_id=None)
    try:
        history: list = []
        asyncio.run(run_turn(ctx, history, max_iterations=5))
    finally:
        loop_mod._llm_for_role = orig_llm

    events = session_log.read_events(session["id"])
    types = [e.type for e in events]
    assert session_log.TOOL_RESULT in types, types
    tr_idx = max(i for i, t in enumerate(types) if t == session_log.TOOL_RESULT)
    end_idx = max(i for i, t in enumerate(types) if t == session_log.TURN_END)
    assert tr_idx < end_idx, "tool/result must precede turn/end"
    tr = events[tr_idx]
    assert tr.data["tool_name"] == "ask_user"
    assert tr.data["result"]["options"] == ["Yes, replace", "No, keep"]
    assert tr.data["result"]["question"] == "Regenerate the script?"
    end = events[end_idx]
    assert end.data["status"] == "awaiting_input", end.data
