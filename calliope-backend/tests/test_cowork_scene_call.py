"""Scene calls close on a new scenes.id, not on the word Done."""

from __future__ import annotations

import asyncio
import json

import pytest

from calliope.agent.capabilities import scenes
from calliope.agent.llm import ChatTurn, ToolCall
from calliope.agent.runtime import subagent as subagent_mod
from calliope.agent.runtime.main_agent import handle, requires_scene_write
from calliope.agent.runtime.task_completion import assess_task_completion
from calliope.config import settings
from calliope.db import get_db, migrate_db
from calliope.main import create_app


class Scripted:
    def __init__(self, turns: list[ChatTurn]) -> None:
        self.turns = turns
        self.prompts: list[str] = []

    def __call__(self, *_args, **_kwargs):
        return self

    async def chat(self, messages, tools=None, *, temperature=0.2):
        del tools, temperature
        self.prompts.append(
            "\n".join(
                str(item.get("content") or "")
                for item in messages
                if item.get("role") in ("system", "user")
            )
        )
        if not self.turns:
            raise AssertionError("the scripted model ran out of replies")
        return self.turns.pop(0)

    async def chat_with_tools(self, messages, temperature=0.7, tools=None, tool_choice=None, on_token=None, on_reasoning=None, on_usage=None):
        turn = await self.chat(messages, tools)
        return {
            "role": "assistant",
            "content": turn.content or "",
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    },
                }
                for call in turn.tool_calls
            ],
        }

    async def close(self) -> None:
        return None


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "assets_dir", tmp_path / "assets")
    asyncio.run(migrate_db(settings.db_path))
    assert settings.db_path.is_relative_to(tmp_path)
    description = "The full beat. " + ("valley " * 400)
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO projects (title, idea) VALUES ('BUTCHERS', 'a valley at dawn')"
        )
        project_id = int(cur.lastrowid)
        conn.execute(
            """
            INSERT INTO story_beats (project_id, order_index, title, description)
            VALUES (?, 4, 'The Craft, Beautiful', ?)
            """,
            (project_id, description),
        )
        for order, heading in (
            (1, "EXT. KESSELBACH VALLEY - PRE-DAWN"),
            (2, "INT. THE RENDER - PRE-DAWN"),
        ):
            conn.execute(
                """
                INSERT INTO scenes (project_id, order_index, heading, action)
                VALUES (?, ?, ?, 'Existing scene.')
                """,
                (project_id, order, heading),
            )
        cur = conn.execute(
            """
            INSERT INTO agent_sessions (project_id, title, status)
            VALUES (?, 'Regenerate Script', 'idle')
            """,
            (project_id,),
        )
        session_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    return {
        "project_id": project_id,
        "session_id": session_id,
        "description": description,
    }


def _count_scenes(project_id: int) -> int:
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM scenes WHERE project_id = ?", (project_id,)
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def test_app_imports():
    from calliope.authoring.projects import idea_meta

    app = create_app()
    assert app.title == "Calliope"
    assert idea_meta("a valley")["is_long"] is False
    assert idea_meta("x" * 2001)["is_long"] is True


def test_done_is_not_completion():
    assert assess_task_completion(has_final_answer=False).reason == "incomplete_response"
    assert assess_task_completion(has_final_answer=True).ready is True
    assert requires_scene_write("How many scenes?") is False
    assert requires_scene_write("create Scene 5") is True


def test_get_beat_returns_the_full_text(board):
    listed = scenes.list_beats(board["project_id"])
    assert listed["beats"][0]["title"] == "The Craft, Beautiful"
    assert "description" not in listed["beats"][0]
    beat = scenes.get_beat(board["project_id"], order=4)
    assert beat["beat"]["description"] == board["description"]
    refused = scenes.add_scene(board["project_id"], heading="  ", action="")
    assert refused["ok"] is False
    assert _count_scenes(board["project_id"]) == 2


@pytest.mark.asyncio
async def test_done_without_add_scene_fails_and_retries(board, monkeypatch):
    scripted = Scripted([ChatTurn(content="Done.")] * 3)
    monkeypatch.setattr(subagent_mod, "LLMClient", scripted)
    result = await handle(
        board["session_id"],
        board["project_id"],
        "Create a scene from beat 4",
    )
    assert result.call is not None
    assert result.call.status == "failed"
    assert result.call.attempt == 3
    assert "add_scene" not in result.call.tools_ran
    assert result.task_status == "failed"
    assert result.text == "Scene was not created. The project still has 2 scenes."
    assert _count_scenes(board["project_id"]) == 2
    assert len(scripted.prompts) == 3
    assert "add_scene" in scripted.prompts[1]
    assert "scenes=2" in scripted.prompts[1]


@pytest.mark.asyncio
async def test_retry_that_inserts_a_row_names_the_new_id(board, monkeypatch):
    scripted = Scripted(
        [
            ChatTurn(content="Done."),
            ChatTurn(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_add",
                        name="add_scene",
                        arguments={
                            "heading": "EXT. YARD - DAY",
                            "action": "She crosses the yard.",
                            "dialog": "MARA: Morning.",
                            "beat_id": 1,
                        },
                    )
                ],
            ),
            ChatTurn(content="Done."),
        ]
    )
    monkeypatch.setattr(subagent_mod, "LLMClient", scripted)
    result = await handle(
        board["session_id"],
        board["project_id"],
        "Create a scene from beat 4",
    )
    assert result.call is not None
    assert result.call.status == "succeeded"
    assert "add_scene" in result.call.tools_ran
    assert result.task_status == "verified"
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            """
            SELECT id, order_index, heading FROM scenes
            WHERE project_id = ? ORDER BY id DESC LIMIT 1
            """,
            (board["project_id"],),
        ).fetchone()
        clips = conn.execute(
            "SELECT COUNT(*) FROM clips WHERE scene_id = ?", (row["id"],)
        ).fetchone()
    finally:
        conn.close()
    assert _count_scenes(board["project_id"]) == 3
    assert int(clips[0]) == 1
    assert result.text == f"Created scene {row['id']} (#{row['order_index']} {row['heading']})."
    assert "add_scene" in scripted.prompts[1]


@pytest.mark.asyncio
async def test_scene_count_question_does_not_write(board, monkeypatch):
    scripted = Scripted([ChatTurn(content="There are 2 scenes.")])
    monkeypatch.setattr(subagent_mod, "LLMClient", scripted)
    result = await handle(
        board["session_id"],
        board["project_id"],
        "How many scenes?",
    )
    assert result.call is not None
    assert result.call.status == "succeeded"
    assert result.call.tools_ran == []
    assert result.text == "The project has 2 scenes."
    assert _count_scenes(board["project_id"]) == 2
    assert len(scripted.prompts) == 1


@pytest.mark.asyncio
async def test_runner_settles_idle_and_records_the_failure(board, monkeypatch):
    from calliope.agent.harness import log as session_log
    from calliope.agent.harness.runner import runner

    scripted = Scripted([ChatTurn(content="Done.")] * 3)
    monkeypatch.setattr(subagent_mod, "LLMClient", scripted)
    message = await runner.start_turn(board["session_id"], "Create a scene from beat 4")
    assert message["role"] == "user"
    task = runner._tasks[board["session_id"]]
    await task
    assert runner.is_running(board["session_id"]) is False
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT status FROM agent_sessions WHERE id = ?", (board["session_id"],)
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "idle"
    plan = session_log.derive_plan(session_log.read_events(board["session_id"]))
    assert plan is not None
    assert plan["tasks"][0]["status"] == "failed"
    rows = session_log.derive_chat_rows(session_log.read_events(board["session_id"]))
    assert rows[-1]["content"] == "Scene was not created. The project still has 2 scenes."
    assert _count_scenes(board["project_id"]) == 2
