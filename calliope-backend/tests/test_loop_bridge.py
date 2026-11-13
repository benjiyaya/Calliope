"""JSON-action bridge: no-function-calling endpoints can still act by writing
a {"tool": name, "arguments": {...}} block — parsed, executed through the SAME
path as a real tool call, capped per turn."""

from __future__ import annotations

import asyncio
import json

import pytest

from calliope.agent.llm import ChatTurn
from calliope.agent.runtime import subagent as subagent_mod
from calliope.agent.runtime.subagent import _bridge_action, _NO_FUNCTION_CALLING_NOTE, run_subagent
from calliope.config import settings
from calliope.db import migrate_db


def test_extract_fenced_action():
    text = 'Let me do that.\n```json\n{"tool": "list_memories", "arguments": {}}\n```'
    action = _bridge_action(text, 1)
    assert action is not None
    assert action.name == "list_memories"
    assert action.arguments == {}
    assert action.id == "bridge_1"


def test_extract_bare_object_action():
    action = _bridge_action('{"tool": "save_memory", "arguments": {"content": "pref"}}', 2)
    assert action is not None
    assert action.name == "save_memory"
    assert action.arguments == {"content": "pref"}


def test_extract_returns_none_for_prose():
    assert _bridge_action("All done — the board is ready.", 1) is None
    assert _bridge_action("", 1) is None


def test_extract_rejects_non_string_tool():
    assert _bridge_action('{"tool": 42, "arguments": {}}', 1) is None
    assert _bridge_action('{"arguments": {}}', 1) is None


def test_extract_coerces_non_dict_arguments():
    action = _bridge_action('{"tool": "list_memories", "arguments": "oops"}', 1)
    assert action is not None
    assert action.arguments == {}


class Scripted:
    def __init__(self, turns: list[str]) -> None:
        self.turns = turns
        self.count = 0

    def __call__(self, *_args, **_kwargs):
        return self

    async def chat_with_tools(
        self, messages, temperature=0.7, tools=None, tool_choice=None,
        on_token=None, on_reasoning=None, on_usage=None,
    ):
        self.count += 1
        return {"role": "assistant", "content": self.turns.pop(0) if self.turns else "", "tool_calls": []}

    async def close(self) -> None:
        return None


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "assets_dir", tmp_path / "assets")
    asyncio.run(migrate_db(settings.db_path))


def test_bridge_executes_action_through_catalog(db, monkeypatch):
    client = Scripted(
        ['```json\n{"tool": "save_memory", "arguments": {"content": "likes fog"}}\n```', "Saved."]
    )
    monkeypatch.setattr(subagent_mod, "LLMClient", client)

    async def scenario():
        return await run_subagent(
            project_id=None, session_id=None, grant="script", goal="remember this"
        )

    result = asyncio.run(scenario())
    assert result.finished is True
    assert "save_memory" in result.tools_ran
    # The bridged action ran through catalog.execute — the memory row exists.
    from calliope.agent.capabilities.memory import list_memories

    rows = list_memories(None, None)["memories"]
    assert any("likes fog" in row["content"] for row in rows)


def test_bridge_respects_grant_check(db, monkeypatch):
    """A bridged call for a tool outside the grant is denied like any other."""
    client = Scripted(
        ['```json\n{"tool": "enqueue_video_jobs", "arguments": {}}\n```', "tried"]
    )
    monkeypatch.setattr(subagent_mod, "LLMClient", client)

    async def scenario():
        return await run_subagent(
            project_id=None, session_id=None, grant="canvas", goal="sneak"
        )

    result = asyncio.run(scenario())
    assert "enqueue_video_jobs" in result.tools_ran
    # The transcript's tool row carries the denial.
    tool_rows = [m for m in result.transcript if m.get("role") == "tool"]
    assert tool_rows, "the bridged call should still log a tool result"
    assert "not granted" in tool_rows[-1]["content"]


def test_bridge_cap_stops_parse_loop(db, monkeypatch):
    looping = '```json\n{"tool": "list_memories", "arguments": {}}\n```'
    replies = [looping] * 9 + ["giving up"]
    client = Scripted(replies)
    monkeypatch.setattr(subagent_mod, "LLMClient", client)
    monkeypatch.setattr(settings, "agent_max_steps", 12)

    async def scenario():
        return await run_subagent(
            project_id=None, session_id=None, grant="script", goal="loop"
        )

    result = asyncio.run(scenario())
    # 8 bridges ran; the 9th identical reply ends the turn with the teaching note.
    assert result.tools_ran.count("list_memories") == 8
    assert _NO_FUNCTION_CALLING_NOTE in result.text


def test_raw_json_without_action_gets_note(db, monkeypatch):
    client = Scripted(['{"result": "not a tool call"}'])
    monkeypatch.setattr(subagent_mod, "LLMClient", client)

    async def scenario():
        return await run_subagent(
            project_id=None, session_id=None, grant="script", goal="go"
        )

    result = asyncio.run(scenario())
    assert _NO_FUNCTION_CALLING_NOTE in result.text
    assert result.finished is True


def test_plain_prose_final_untouched(db, monkeypatch):
    client = Scripted(["The board is ready for review."])
    monkeypatch.setattr(subagent_mod, "LLMClient", client)

    async def scenario():
        return await run_subagent(
            project_id=None, session_id=None, grant="script", goal="go"
        )

    result = asyncio.run(scenario())
    assert result.text == "The board is ready for review."
    assert _NO_FUNCTION_CALLING_NOTE not in result.text
