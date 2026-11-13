"""Grant scoping: a subagent can call the tools in its grant and nothing else.

The payload (tools_for) and the execute path (run_subagent's grant check) are
two layers of the same rule; the coordinator refuses project grants without a
project, and the memory/skills base is in EVERY grant.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from calliope.agent.capabilities.catalog import GRANTS, tools_for
from calliope.agent.llm import ChatTurn, ToolCall
from calliope.agent.runtime import subagent as subagent_mod
from calliope.agent.runtime.coordinator import dispatch
from calliope.agent.runtime.subagent import run_subagent
from calliope.agent.runtime.types import Run
from calliope.config import settings
from calliope.db import migrate_db


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "assets_dir", tmp_path / "assets")
    asyncio.run(migrate_db(settings.db_path))


class Scripted:
    def __init__(self, turns: list[ChatTurn]) -> None:
        self.turns = turns

    def __call__(self, *_args, **_kwargs):
        return self

    async def chat_with_tools(
        self, messages, temperature=0.7, tools=None, tool_choice=None,
        on_token=None, on_reasoning=None, on_usage=None,
    ):
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


# ── payload layer ──────────────────────────────────────────────────────────


def test_memory_and_skills_base_in_every_grant():
    base = {"save_memory", "list_memories", "forget_memory", "list_skills", "read_skill", "ask_user"}
    for name, tools in GRANTS.items():
        assert base <= tools, f"{name} grant lost the universal base"


def test_system_tools_in_chat_grants_not_shot():
    system = {"run_command", "list_files", "read_file", "write_file", "delete_file"}
    for name in ("story", "script", "assets", "render", "canvas"):
        assert system <= GRANTS[name], f"{name} grant lost the system tools"
    assert not (system & GRANTS["shot"]), "Build Scene must stay shot-only"


def test_grant_boundaries():
    assert "add_beat" not in GRANTS["script"]
    assert "add_object" not in GRANTS["script"]
    assert "enqueue_video_jobs" not in GRANTS["script"]
    assert "add_scene" not in GRANTS["render"]
    assert "enqueue_video_jobs" in GRANTS["render"]
    assert "create_project" in GRANTS["story"]
    assert "run_workflow" not in GRANTS["script"]


def test_tools_for_returns_exactly_the_grant():
    names = {entry["function"]["name"] for entry in tools_for("script")}
    assert names == set(GRANTS["script"])
    shot_names = {entry["function"]["name"] for entry in tools_for("shot")}
    assert shot_names == set(GRANTS["shot"])


# ── execute layer ──────────────────────────────────────────────────────────


def test_subagent_denies_out_of_grant_calls():
    client = Scripted(
        [
            ChatTurn(content=None, tool_calls=[ToolCall(id="c1", name="enqueue_video_jobs", arguments={})]),
            ChatTurn(content="tried"),
        ]
    )
    monkeypatch_target = subagent_mod
    original = monkeypatch_target.LLMClient
    monkeypatch_target.LLMClient = client  # patched below via monkeypatch fixture

    async def scenario():
        return await run_subagent(
            project_id=None, session_id=None, grant="canvas", goal="sneak"
        )

    import unittest.mock

    with unittest.mock.patch.object(subagent_mod, "LLMClient", client):
        result = asyncio.run(scenario())
    assert "enqueue_video_jobs" in result.tools_ran
    tool_rows = [m for m in result.transcript if m.get("role") == "tool"]
    assert "not granted" in tool_rows[-1]["content"]
    del original


def test_coordinator_refuses_project_grants_without_project():
    run = Run(id="r1", session_id=1, project_id=None, goal="go")

    async def scenario():
        return await dispatch(run, "go", grant="script", write_required=False)

    with pytest.raises(RuntimeError, match="needs a project"):
        asyncio.run(scenario())


def test_coordinator_allows_canvas_without_project():
    """Sandbox chats may run canvas calls (the sandbox canvas is keyed by the
    session row) — dispatch must not raise "needs a project"."""
    from unittest.mock import patch

    from calliope.db import get_db

    conn = get_db(settings.db_path)
    try:
        conn.execute(
            "INSERT INTO agent_sessions (id, title, origin) VALUES (1, 'sandbox', 'chat')"
        )
        conn.commit()
    finally:
        conn.close()
    run = Run(id="r2", session_id=1, project_id=None, goal="go")

    class FailClient:
        def __call__(self, *_a, **_k):
            return self

        async def chat_with_tools(self, messages, **kwargs):
            return {"role": "assistant", "content": "no tools here", "tool_calls": []}

        async def close(self):
            return None

    with patch.object(subagent_mod, "LLMClient", FailClient()):
        # A finished reply without writes fails the fingerprint check — but
        # the POINT is it did not raise "needs a project".
        call, _ = asyncio.run(
            dispatch(run, "go", grant="canvas", write_required=False)
        )
        assert call.status in ("failed", "succeeded")
