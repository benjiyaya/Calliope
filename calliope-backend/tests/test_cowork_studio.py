"""Studio grants close on a table change, and each call sees only its tools."""

from __future__ import annotations

import asyncio
import json

import pytest

from calliope.agent.capabilities import catalog
from calliope.agent.llm import ChatTurn, ToolCall
from calliope.agent.runtime import subagent as subagent_mod
from calliope.agent.runtime.main_agent import handle, schedule
from calliope.config import settings
from calliope.db import get_db, migrate_db


class Scripted:
    def __init__(self, turns: list[ChatTurn]) -> None:
        self.turns = turns
        self.tools: list[list[str]] = []
        self.prompts: list[str] = []

    def __call__(self, *_args, **_kwargs):
        return self

    async def chat(self, messages, tools=None, *, temperature=0.2):
        del temperature
        self.prompts.append(
            "\n".join(
                str(item.get("content") or "")
                for item in messages
                if item.get("role") in ("system", "user")
            )
        )
        names = []
        for tool in tools or []:
            names.append(tool["function"]["name"])
        self.tools.append(names)
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
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO projects (title, idea) VALUES ('BUTCHERS', 'a valley at dawn')"
        )
        project_id = int(cur.lastrowid)
        conn.execute(
            """
            INSERT INTO story_beats (project_id, order_index, title, description)
            VALUES (?, 1, 'Dawn', 'The valley wakes.')
            """,
            (project_id,),
        )
        cur = conn.execute(
            """
            INSERT INTO scenes (project_id, order_index, heading, action)
            VALUES (?, 1, 'EXT. VALLEY', 'Existing scene.')
            """,
            (project_id,),
        )
        scene_id = int(cur.lastrowid)
        conn.execute(
            """
            INSERT INTO clips (
                scene_id, project_id, order_index, description, shot_size, duration_sec
            )
            VALUES (?, ?, 1, 'Wide valley.', 'wide', 8)
            """,
            (scene_id, project_id),
        )
        cur = conn.execute(
            """
            INSERT INTO agent_sessions (project_id, title, status, origin)
            VALUES (?, 'Studio', 'idle', 'chat')
            """,
            (project_id,),
        )
        session_id = int(cur.lastrowid)
        cur = conn.execute(
            """
            INSERT INTO agent_sessions (project_id, title, status, origin)
            VALUES (NULL, 'Scene 1', 'idle', 'scene')
            """
        )
        shot_session_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    return {
        "project_id": project_id,
        "session_id": session_id,
        "scene_id": scene_id,
        "shot_session_id": shot_session_id,
    }


@pytest.mark.asyncio
async def test_sandbox_project_creation_links_the_chat(board, monkeypatch):
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO agent_sessions (project_id, title, status, origin)
            VALUES (NULL, 'New chat', 'idle', 'chat')
            """
        )
        session_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()

    async def fake_story(project_id, replace=False):
        conn = get_db(settings.db_path)
        try:
            conn.execute(
                """
                INSERT INTO story_beats (project_id, order_index, title, description)
                VALUES (?, 1, 'Dawn', 'Fog in the valley.')
                """,
                (project_id,),
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "beats": 1, "project_id": project_id}

    async def fake_script(project_id, replace=False, scene_count=None, with_clips=True):
        conn = get_db(settings.db_path)
        try:
            cur = conn.execute(
                """
                INSERT INTO scenes (project_id, order_index, heading, action)
                VALUES (?, 1, 'EXT. VALLEY', 'The fog holds.')
                """,
                (project_id,),
            )
            scene_id = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO clips (
                    scene_id, project_id, order_index, description, shot_size, duration_sec
                )
                VALUES (?, ?, 1, 'Wide fog.', 'wide', 8)
                """,
                (scene_id, project_id),
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "scenes": [{"id": scene_id}], "project_id": project_id}

    monkeypatch.setattr("calliope.routers.story.generate_story", fake_story)
    monkeypatch.setattr("calliope.agent.script_agent.generate_script", fake_script)
    scripted = Scripted(
        [
            ChatTurn(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_project",
                        name="create_project",
                        arguments={"title": "Fog Valley", "idea": "a valley at dawn"},
                    ),
                    ToolCall(id="call_story", name="generate_story", arguments={}),
                ],
            ),
            ChatTurn(content="Done."),
            ChatTurn(
                content="",
                tool_calls=[ToolCall(id="call_script", name="generate_script", arguments={})],
            ),
            ChatTurn(content="Done."),
        ]
    )
    monkeypatch.setattr(subagent_mod, "LLMClient", scripted)
    result = await handle(session_id, None, "Turn this story idea into a script")
    assert result.task_status == "verified"
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT project_id, title FROM agent_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        project_id = int(row["project_id"])
        beats = conn.execute(
            "SELECT COUNT(*) FROM story_beats WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0]
        scenes = conn.execute(
            "SELECT COUNT(*) FROM scenes WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0]
        canvas = conn.execute(
            "SELECT id FROM canvas WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["title"] == "Fog Valley"
    assert int(beats) == 1
    assert int(scenes) == 1
    assert canvas is not None


def test_builtin_skills_stop_at_script_and_lock_the_camera():
    listed = asyncio.run(catalog.execute("list_skills", {}, None, None))
    names = {row["name"] for row in listed["skills"]}
    assert "project-creation" in names
    assert "video-continuation" in names
    project = asyncio.run(catalog.execute("read_skill", {"name": "project-creation"}, None, None))
    assert "stop" in project["content"].lower()
    assert "enqueue_video_jobs" in project["content"]
    video = asyncio.run(catalog.execute("read_skill", {"name": "video-continuation"}, None, None))
    assert "camera" in video["content"].lower()
    assert "place" in video["content"].lower()


@pytest.mark.asyncio
async def test_retry_continues_from_the_tool_transcript(board, monkeypatch):
    monkeypatch.setattr(settings, "agent_max_steps", 1)
    seen: list[list[dict]] = []

    class Recording(Scripted):
        async def chat(self, messages, tools=None, *, temperature=0.2):
            seen.append(list(messages))
            return await super().chat(messages, tools, temperature=temperature)

    scripted = Recording(
        [
            ChatTurn(
                content="",
                tool_calls=[ToolCall(id="call_list", name="list_scenes", arguments={})],
            ),
            ChatTurn(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_add",
                        name="add_scene",
                        arguments={"heading": "EXT. CONTINUED", "action": "The valley holds."},
                    )
                ],
            ),
        ]
    )
    monkeypatch.setattr(subagent_mod, "LLMClient", scripted)
    result = await handle(
        board["session_id"], board["project_id"], "Create a scene from beat 4"
    )
    assert result.call is not None
    assert result.call.status == "succeeded"
    assert len(seen) >= 2
    assert any(item.get("role") == "tool" for item in seen[1])
    assert any("Recovery:" in str(item.get("content") or "") for item in seen[1])


def test_schedule_picks_one_grant():
    scene = schedule("Create a scene from beat 4")
    assert [(item.grant, item.write_required, item.required_tool) for item in scene] == [
        ("script", True, "add_scene")
    ]
    count = schedule("How many scenes?")
    assert count[0].grant == "script" and count[0].write_required is False
    beat = schedule("Add a beat")
    assert beat[0].grant == "story" and beat[0].required_tool == "add_beat"
    shots = schedule("Break scene 2 into shots")
    assert shots[0].required_tool == "break_into_shots"
    render = schedule("render scene 1")
    assert [item.grant for item in render] == ["render"]
    assert schedule("Add a block", origin="scene")[0].grant == "shot"
    assert all(item.grant != "shot" for item in schedule("Add a block"))
    preview = schedule("Preview the clip prompt")
    assert [(item.grant, item.write_required) for item in preview] == [("render", False)]
    script = schedule("Write the script")
    assert script[0].grant == "script" and script[0].required_tool == "generate_script"
    created = schedule("Turn this story idea into a script")
    assert [(item.grant, item.required_tool) for item in created] == [
        ("story", "generate_story"),
        ("script", "generate_script"),
    ]


def test_empty_joint_values_are_refused(board):
    from calliope.agent.capabilities import shot_ops

    added = shot_ops.execute(
        board["shot_session_id"],
        "add_object",
        {"id": "fig", "kind": "figure"},
    )
    assert added["ok"] is True
    refused = shot_ops.execute(
        board["shot_session_id"],
        "set_joint",
        {"object_id": "fig", "joint": "hip", "values": []},
    )
    assert refused["ok"] is False


def test_script_grant_excludes_shot_tools():
    script = {tool["function"]["name"] for tool in catalog.tools_for("script")}
    shot = {tool["function"]["name"] for tool in catalog.tools_for("shot")}
    assert "add_scene" in script
    assert "add_beat" not in script
    assert "add_object" not in script
    assert "add_scene" not in shot
    assert "enqueue_video_jobs" not in script


def test_update_scene_and_last_clip(board):
    updated = catalog_sync_update(board)
    assert updated["ok"] is True
    refused = asyncio.run(
        catalog.execute(
            "delete_clip",
            {"ref": "#1.1"},
            board["project_id"],
            board["session_id"],
        )
    )
    assert refused["ok"] is False
    assert "delete_scene" in refused["error"]


def catalog_sync_update(board):
    return asyncio.run(
        catalog.execute(
            "update_scene",
            {"scene_id": board["scene_id"], "heading": "EXT. VALLEY - DAY"},
            board["project_id"],
            board["session_id"],
        )
    )


@pytest.mark.asyncio
async def test_add_beat_succeeds_only_when_the_row_exists(board, monkeypatch):
    scripted = Scripted(
        [
            ChatTurn(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_beat",
                        name="add_beat",
                        arguments={"title": "The yard", "description": "She crosses."},
                    )
                ],
            ),
            ChatTurn(content="Done."),
        ]
    )
    monkeypatch.setattr(subagent_mod, "LLMClient", scripted)
    result = await handle(board["session_id"], board["project_id"], "Add a beat")
    assert result.call is not None
    assert result.call.status == "succeeded"
    assert result.task_status == "verified"
    assert "add_beat" in scripted.tools[0]
    assert "add_scene" not in scripted.tools[0]
    assert "Created scene" not in result.text
    conn = get_db(settings.db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM story_beats WHERE project_id = ?",
            (board["project_id"],),
        ).fetchone()[0]
    finally:
        conn.close()
    assert int(count) == 2


@pytest.mark.asyncio
async def test_beat_done_does_not_count(board, monkeypatch):
    scripted = Scripted([ChatTurn(content="Done.")] * 3)
    monkeypatch.setattr(subagent_mod, "LLMClient", scripted)
    result = await handle(board["session_id"], board["project_id"], "Add a beat")
    assert result.call is not None
    assert result.call.status == "failed"
    assert result.call.attempt == 3
    assert result.text == "The story was not changed. The project still has 1 beat."
    assert "add_beat" in scripted.prompts[1]
    assert "beats=1" in scripted.prompts[1]
    conn = get_db(settings.db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM story_beats WHERE project_id = ?",
            (board["project_id"],),
        ).fetchone()[0]
    finally:
        conn.close()
    assert int(count) == 1


@pytest.mark.asyncio
async def test_shot_session_writes_the_composition(board, monkeypatch):
    scripted = Scripted(
        [
            ChatTurn(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_obj",
                        name="add_object",
                        arguments={"id": "crate", "name": "Crate", "kind": "block"},
                    )
                ],
            ),
            ChatTurn(content="Done."),
        ]
    )
    monkeypatch.setattr(subagent_mod, "LLMClient", scripted)
    result = await handle(
        board["shot_session_id"],
        None,
        "Add a block named Crate",
        origin="scene",
    )
    assert result.call is not None
    assert result.call.status == "succeeded"
    assert "add_scene" not in scripted.tools[0]
    assert "add_object" in scripted.tools[0]
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT scene_json FROM shot_composition WHERE agent_session_id = ?",
            (board["shot_session_id"],),
        ).fetchone()
    finally:
        conn.close()
    data = json.loads(row["scene_json"])
    assert data["objects"][0]["id"] == "crate"


@pytest.mark.asyncio
async def test_canvas_node_is_the_success_check(board, monkeypatch):
    scripted = Scripted(
        [
            ChatTurn(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_node",
                        name="create_canvas_node",
                        arguments={"type": "text", "title": "Note"},
                    )
                ],
            ),
            ChatTurn(content="Done."),
        ]
    )
    monkeypatch.setattr(subagent_mod, "LLMClient", scripted)
    result = await handle(board["session_id"], board["project_id"], "Add a canvas note")
    assert result.call is not None
    assert result.call.status == "succeeded"
    assert "create_canvas_node" in scripted.tools[0]
    assert "add_scene" not in scripted.tools[0]
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            """
            SELECT canvas_node.title FROM canvas_node
            JOIN canvas ON canvas.id = canvas_node.canvas_id
            WHERE canvas.project_id = ?
            """,
            (board["project_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row["title"] == "Note"


def test_cast_rows_round_trip(board):
    created = asyncio.run(
        catalog.execute(
            "add_character",
            {"name": "Mara", "role": "lead"},
            board["project_id"],
            board["session_id"],
        )
    )
    assert created["ok"] is True
    character_id = created["character"]["id"]
    edited = asyncio.run(
        catalog.execute(
            "update_character",
            {"character_id": character_id, "appearance": "yellow jacket"},
            board["project_id"],
            board["session_id"],
        )
    )
    assert edited["ok"] is True
    place = asyncio.run(
        catalog.execute(
            "add_location",
            {"name": "Valley", "description": "pre-dawn"},
            board["project_id"],
            board["session_id"],
        )
    )
    prop = asyncio.run(
        catalog.execute(
            "add_item",
            {"name": "Lantern"},
            board["project_id"],
            board["session_id"],
        )
    )
    assert place["ok"] is True and prop["ok"] is True
    removed = asyncio.run(
        catalog.execute(
            "delete_character",
            {"character_id": character_id},
            board["project_id"],
            board["session_id"],
        )
    )
    assert removed["ok"] is True


def _insert_job(project_id: int, kind: str) -> None:
    conn = get_db(settings.db_path)
    try:
        conn.execute(
            "INSERT INTO jobs (project_id, kind, status) VALUES (?, ?, 'pending')",
            (project_id, kind),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_asset_and_render_calls_need_a_job_row(board, monkeypatch):
    async def fake_assets(project_id, **_kwargs):
        _insert_job(project_id, "image")
        return [{"id": 1}]

    async def fake_video(project_id, **_kwargs):
        _insert_job(project_id, "video")
        return [{"id": 2}]

    monkeypatch.setattr("calliope.agent.asset_agent.enqueue_asset_jobs", fake_assets)
    monkeypatch.setattr("calliope.agent.video_agent.enqueue_video_jobs", fake_video)
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO characters (project_id, name, appearance)
            VALUES (?, 'Mara', 'a butcher at dawn')
            """,
            (board["project_id"],),
        )
        character_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    portrait = Scripted(
        [
            ChatTurn(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_asset",
                        name="enqueue_asset_jobs",
                        arguments={"all_missing": True},
                    )
                ],
            ),
            ChatTurn(content="Done."),
        ]
    )
    monkeypatch.setattr(subagent_mod, "LLMClient", portrait)
    asset_result = await handle(
        board["session_id"], board["project_id"], "Generate a portrait"
    )
    assert asset_result.call is not None
    assert asset_result.call.status == "blocked"
    assert "Review the prompt" in asset_result.text
    assert "enqueue_asset_jobs" in portrait.tools[0]
    assert "add_scene" not in portrait.tools[0]

    missed = Scripted([ChatTurn(content="Done.")] * 3)
    monkeypatch.setattr(subagent_mod, "LLMClient", missed)
    missed_result = await handle(
        board["session_id"], board["project_id"], "render scene 1"
    )
    assert missed_result.call is not None
    assert missed_result.call.status == "failed"
    assert "enqueue_video_jobs" in missed.prompts[1]
    assert "jobs=" in missed.prompts[1]

    render = Scripted(
        [
            ChatTurn(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_video",
                        name="enqueue_video_jobs",
                        arguments={"clip_ids": [1]},
                    )
                ],
            ),
            ChatTurn(content="Done."),
        ]
    )
    monkeypatch.setattr(subagent_mod, "LLMClient", render)
    render_result = await handle(
        board["session_id"], board["project_id"], "render scene 1"
    )
    assert render_result.call is not None
    assert render_result.call.status == "blocked"
    assert "Review the prompt" in render_result.text
    assert "enqueue_video_jobs" in render.tools[0]
    assert "add_scene" not in render.tools[0]
    conn = get_db(settings.db_path)
    try:
        kinds = [
            row[0]
            for row in conn.execute(
                "SELECT kind FROM jobs WHERE project_id = ? ORDER BY id",
                (board["project_id"],),
            ).fetchall()
        ]
    finally:
        conn.close()
    assert kinds == []
    from calliope.agent.capabilities.review import commit_review

    queued_image = await commit_review(
        board["project_id"],
        board["session_id"],
        "image",
        [{"id": character_id, "entity": "character", "prompt": "Reviewed face at dawn"}],
    )
    assert queued_image["ok"] is True
    clip_id = None
    conn = get_db(settings.db_path)
    try:
        clip_id = int(
            conn.execute(
                "SELECT id FROM clips WHERE project_id = ?",
                (board["project_id"],),
            ).fetchone()[0]
        )
        saved = conn.execute(
            "SELECT consistency_prompt FROM characters WHERE id = ?",
            (character_id,),
        ).fetchone()
        assert saved["consistency_prompt"] == "Reviewed face at dawn"
    finally:
        conn.close()
    queued_video = await commit_review(
        board["project_id"],
        board["session_id"],
        "video",
        [{"id": clip_id, "entity": "clip", "prompt": "Locked wide eye-level valley"}],
    )
    assert queued_video["ok"] is True
    conn = get_db(settings.db_path)
    try:
        kinds = [
            row[0]
            for row in conn.execute(
                "SELECT kind FROM jobs WHERE project_id = ? ORDER BY id",
                (board["project_id"],),
            ).fetchall()
        ]
        draft = conn.execute(
            "SELECT video_settings_json FROM clips WHERE id = ?",
            (clip_id,),
        ).fetchone()
    finally:
        conn.close()
    assert kinds == ["image", "video"]
    assert "Locked wide eye-level valley" in draft["video_settings_json"]


@pytest.mark.asyncio
async def test_generate_script_needs_a_new_scene_row(board, monkeypatch):
    from calliope.agent.capabilities.scenes import add_scene

    async def fake(project_id, **_kwargs):
        add_scene(project_id, heading="EXT. NEW", action="She waits.")
        return {"ok": True}

    monkeypatch.setattr("calliope.agent.script_agent.generate_script", fake)
    missed = Scripted([ChatTurn(content="Done.")] * 3)
    monkeypatch.setattr(subagent_mod, "LLMClient", missed)
    missed_result = await handle(board["session_id"], board["project_id"], "Write the script")
    assert missed_result.call is not None
    assert missed_result.call.status == "failed"
    assert missed_result.text == "The script was not changed. The project still has 1 scene."
    assert "generate_script" in missed.prompts[1]
    assert "scenes=1" in missed.prompts[1]

    wrote = Scripted(
        [
            ChatTurn(
                content="",
                tool_calls=[
                    ToolCall(id="call_script", name="generate_script", arguments={})
                ],
            ),
            ChatTurn(content="Done."),
        ]
    )
    monkeypatch.setattr(subagent_mod, "LLMClient", wrote)
    result = await handle(board["session_id"], board["project_id"], "Write the script")
    assert result.call is not None
    assert result.call.status == "succeeded"
    assert result.text.startswith("Created scene ")
    assert "generate_script" in wrote.tools[0]


@pytest.mark.asyncio
async def test_story_draft_and_shot_break_need_table_changes(board, monkeypatch):
    from calliope.agent.capabilities.beats import add_beat

    async def fake_story(project_id, *, replace=False):
        del replace
        return add_beat(project_id, {"title": "Night", "description": "Dark."})

    async def fake_break(project_id, scene_ids=None, *, guidance=None, clip_cap=None):
        del scene_ids, guidance, clip_cap
        conn = get_db(settings.db_path)
        try:
            conn.execute(
                "UPDATE clips SET description = ? WHERE project_id = ?",
                ("Broken into shots.", project_id),
            )
            conn.commit()
        finally:
            conn.close()
        return {"ok": True, "scenes": []}

    monkeypatch.setattr(
        "calliope.routers.story.generate_story", fake_story
    )
    monkeypatch.setattr(
        "calliope.agent.coverage_agent.expand_scene_coverage", fake_break
    )
    missed = Scripted([ChatTurn(content="Done.")] * 3)
    monkeypatch.setattr(subagent_mod, "LLMClient", missed)
    missed_story = await handle(board["session_id"], board["project_id"], "Draft the story")
    assert missed_story.call is not None
    assert missed_story.call.status == "failed"
    assert "generate_story" in missed.prompts[1]
    assert "beats=1" in missed.prompts[1]

    drafted = Scripted(
        [
            ChatTurn(
                content="",
                tool_calls=[ToolCall(id="call_story", name="generate_story", arguments={})],
            ),
            ChatTurn(content="Done."),
        ]
    )
    monkeypatch.setattr(subagent_mod, "LLMClient", drafted)
    story = await handle(board["session_id"], board["project_id"], "Draft the story")
    assert story.call is not None
    assert story.call.status == "succeeded"
    assert "generate_story" in drafted.tools[0]
    assert "add_scene" not in drafted.tools[0]

    broken = Scripted(
        [
            ChatTurn(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_break",
                        name="break_into_shots",
                        arguments={"scene_id": board["scene_id"]},
                    )
                ],
            ),
            ChatTurn(content="Done."),
        ]
    )
    monkeypatch.setattr(subagent_mod, "LLMClient", broken)
    shots = await handle(
        board["session_id"], board["project_id"], "Break scene 1 into shots"
    )
    assert shots.call is not None
    assert shots.call.status == "succeeded"
    assert "break_into_shots" in broken.tools[0]
    assert "enqueue_video_jobs" not in broken.tools[0]
    conn = get_db(settings.db_path)
    try:
        beats = conn.execute(
            "SELECT COUNT(*) FROM story_beats WHERE project_id = ?",
            (board["project_id"],),
        ).fetchone()[0]
        clip = conn.execute(
            "SELECT description FROM clips WHERE project_id = ?",
            (board["project_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert int(beats) == 2
    assert clip["description"] == "Broken into shots."


def test_edit_delete_and_canvas_edge(board):
    added = asyncio.run(
        catalog.execute(
            "add_beat",
            {"title": "Extra", "description": "A spare beat."},
            board["project_id"],
            board["session_id"],
        )
    )
    beat_id = added["beat"]["id"]
    edited = asyncio.run(
        catalog.execute(
            "update_beat",
            {"beat_id": beat_id, "title": "Later"},
            board["project_id"],
            board["session_id"],
        )
    )
    assert edited["ok"] is True and edited["beat"]["title"] == "Later"
    deleted = asyncio.run(
        catalog.execute(
            "delete_beat",
            {"beat_id": beat_id},
            board["project_id"],
            board["session_id"],
        )
    )
    assert deleted["ok"] is True
    clip = asyncio.run(
        catalog.execute(
            "add_clip",
            {
                "scene_id": board["scene_id"],
                "description": "A second angle.",
                "shot_size": "closeUp",
            },
            board["project_id"],
            board["session_id"],
        )
    )
    assert clip["ok"] is True
    removed_clip = asyncio.run(
        catalog.execute(
            "delete_clip",
            {"clip_id": clip["clip"]["id"]},
            board["project_id"],
            board["session_id"],
        )
    )
    assert removed_clip["ok"] is True
    left = asyncio.run(
        catalog.execute(
            "update_location",
            {"location_id": 999, "name": "Missing"},
            board["project_id"],
            board["session_id"],
        )
    )
    assert left["ok"] is False
    place = asyncio.run(
        catalog.execute(
            "add_location",
            {"name": "Yard"},
            board["project_id"],
            board["session_id"],
        )
    )
    renamed = asyncio.run(
        catalog.execute(
            "update_location",
            {"location_id": place["location"]["id"], "description": "mud"},
            board["project_id"],
            board["session_id"],
        )
    )
    assert renamed["ok"] is True
    item = asyncio.run(
        catalog.execute(
            "add_item",
            {"name": "Rope"},
            board["project_id"],
            board["session_id"],
        )
    )
    gone = asyncio.run(
        catalog.execute(
            "delete_item",
            {"item_id": item["item"]["id"]},
            board["project_id"],
            board["session_id"],
        )
    )
    assert gone["ok"] is True
    src = asyncio.run(
        catalog.execute(
            "create_canvas_node",
            {"type": "text", "title": "From"},
            board["project_id"],
            board["session_id"],
        )
    )
    dst = asyncio.run(
        catalog.execute(
            "create_canvas_node",
            {"type": "workflow", "title": "To", "workflow_id": None},
            board["project_id"],
            board["session_id"],
        )
    )
    edge = asyncio.run(
        catalog.execute(
            "canvas_connect",
            {
                "src_node_id": src["node_id"],
                "dst_node_id": dst["node_id"],
                "dst_role": "prompt",
            },
            board["project_id"],
            board["session_id"],
        )
    )
    assert edge["ok"] is True
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT dst_role FROM canvas_edge WHERE id = ?",
            (edge["edge_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row["dst_role"] == "prompt"


def test_schedule_spans_grants_and_ignores_chatter():
    both = schedule("Edit the beat and update the clip")
    assert [item.grant for item in both] == ["story", "script"]
    assert both[0].required_tool == "update_beat"
    assert both[1].required_tool == "update_clip"
    film = schedule("Create a new film and generate a portrait")
    assert [item.grant for item in film] == ["story", "script", "assets"]
    assert schedule("thanks") == []
    waiting = schedule("when the jobs finish")
    assert [item.required_tool for item in waiting] == ["wait_for_jobs", "wait_for_jobs"]


def test_get_scene_keeps_the_dialogue_list_clips(board):
    conn = get_db(settings.db_path)
    try:
        conn.execute(
            "UPDATE scenes SET action = ?, dialog = ? WHERE id = ?",
            ("A" * 400, "MARA: The fog stays.", board["scene_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    listed = asyncio.run(
        catalog.execute("list_scenes", {}, board["project_id"], board["session_id"])
    )
    full = asyncio.run(
        catalog.execute(
            "get_scene",
            {"scene_id": board["scene_id"]},
            board["project_id"],
            board["session_id"],
        )
    )
    assert "MARA: The fog stays." not in listed["scenes"][0].get("action", "")
    assert full["scene"]["dialog"] == "MARA: The fog stays."
    assert len(full["scene"]["action"]) == 400


def test_image_prompt_and_attach_round_trip(board, tmp_path):
    added = asyncio.run(
        catalog.execute(
            "add_character",
            {"name": "Mara", "consistency_prompt": "black coat, locked"},
            board["project_id"],
            board["session_id"],
        )
    )
    assert added["character"]["consistency_prompt"] == "black coat, locked"
    image = settings.assets_dir / "mara.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"png")
    attached = asyncio.run(
        catalog.execute(
            "attach_image",
            {"entity": "character", "id": added["character"]["id"], "path": str(image)},
            board["project_id"],
            board["session_id"],
        )
    )
    assert attached["ok"] is True
    outside = tmp_path / "nope.png"
    outside.write_bytes(b"png")
    refused = asyncio.run(
        catalog.execute(
            "attach_image",
            {"entity": "character", "id": added["character"]["id"], "path": str(outside)},
            board["project_id"],
            board["session_id"],
        )
    )
    assert refused["ok"] is False


def test_clip_prompt_is_visible_to_the_review(board):
    conn = get_db(settings.db_path)
    try:
        clip_id = conn.execute(
            "SELECT id FROM clips WHERE project_id = ?", (board["project_id"],)
        ).fetchone()["id"]
    finally:
        conn.close()
    saved = asyncio.run(
        catalog.execute(
            "update_clip",
            {
                "clip_id": clip_id,
                "prompt": "same valley, eye-level, locked wide",
                "images": ["valley.png"],
            },
            board["project_id"],
            board["session_id"],
        )
    )
    assert saved["ok"] is True
    from calliope.agent.capabilities.review import video_review

    review = video_review(board["project_id"], {"clip_ids": [clip_id]})
    item = review["review"]["items"][0]
    assert "locked wide" in item["prompt"]
    assert "valley.png" in item["images"]


def test_link_project_moves_a_sandbox_chat(board):
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO agent_sessions (project_id, title, status, origin)
            VALUES (NULL, 'Sandbox', 'idle', 'chat')
            """
        )
        session_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    linked = asyncio.run(
        catalog.execute(
            "link_project",
            {"project_id": board["project_id"]},
            None,
            session_id,
        )
    )
    assert linked["ok"] is True
    assert linked["canvas_id"]
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT project_id FROM agent_sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()
    assert int(row["project_id"]) == board["project_id"]


@pytest.mark.asyncio
async def test_saved_memory_is_in_the_next_prompt(board, monkeypatch):
    saved = await catalog.execute(
        "save_memory",
        {"content": "Mara's coat stays black", "kind": "preference"},
        board["project_id"],
        board["session_id"],
    )
    assert saved["ok"] is True
    scripted = Scripted([ChatTurn(content="The project has scenes.")])
    monkeypatch.setattr(subagent_mod, "LLMClient", scripted)
    await handle(board["session_id"], board["project_id"], "How many scenes?")
    assert "Mara's coat stays black" in scripted.prompts[0]


def test_canvas_post_keeps_the_file_and_run_does_not_queue(board):
    posted = asyncio.run(
        catalog.execute(
            "post_artifact_to_canvas",
            {"title": "Still", "kind": "image", "artifact_path": "E:/assets/still.png"},
            board["project_id"],
            board["session_id"],
        )
    )
    assert posted["ok"] is True
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT artifact_path FROM canvas_node WHERE id = ?",
            (posted["node_id"],),
        ).fetchone()
        jobs = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE project_id = ?",
            (board["project_id"],),
        ).fetchone()[0]
    finally:
        conn.close()
    assert row["artifact_path"] == "E:/assets/still.png"
    reviewed = asyncio.run(
        catalog.execute(
            "run_canvas_node",
            {"node_id": posted["node_id"]},
            board["project_id"],
            board["session_id"],
        )
    )
    assert reviewed["awaiting_review"] is True
    assert int(jobs) == 0


def test_hour_long_story_is_many_short_calls():
    from calliope.agent.prompts import recommend_beat_count
    from calliope.agent.script_agent import _beats_for_chunk

    assert recommend_beat_count("60 minutes") > 60
    beats = [
        {"order_index": index, "title": f"T{index}", "description": f"DESC{index}-ONLY"}
        for index in range(1, 11)
    ]
    window, _arc = _beats_for_chunk(beats, 10, 8, 2)
    descriptions = " ".join(str(beat["description"]) for beat in window)
    assert "DESC1-ONLY" not in descriptions
    assert "DESC8-ONLY" in descriptions


@pytest.mark.asyncio
async def test_agent_story_uses_more_than_one_model_call(board, monkeypatch):
    calls = {"n": 0}

    async def fake_structured(messages, temperature=0.7):
        del messages, temperature
        calls["n"] += 1
        return {
            "title": "Fog",
            "beats": [
                {"order_index": index, "title": f"B{index}", "description": "Fog."}
                for index in range(1, 13)
            ],
            "characters": [],
            "locations": [],
            "items": [],
        }

    monkeypatch.setattr("calliope.routers.story.generate_structured", fake_structured)
    conn = get_db(settings.db_path)
    try:
        conn.execute(
            "UPDATE projects SET target_duration = ? WHERE id = ?",
            ("3 minutes", board["project_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    result = await catalog.execute(
        "generate_story", {"replace": True}, board["project_id"], board["session_id"]
    )
    assert result["ok"] is True
    assert calls["n"] >= 2




