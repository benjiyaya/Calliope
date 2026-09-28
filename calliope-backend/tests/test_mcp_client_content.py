"""MCP client content: the client writes story/script/shots/plan/prompts, Calliope's LLM is never called."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from calliope import mcp_server
from calliope.config import settings

REPO_ROOT = Path(__file__).resolve().parents[2]
R2V_1REF = REPO_ROOT / "example_ComfyUI_workflows" / "video_minimax_h3_r2v_API.json"

H3_REF_PROMPT = """subject_definitions:
<Subject 1> is the keeper "Ada" in <Picture 1>, grey wool coat, silver braid.
summary:
[reference generation] Ada climbs the lighthouse stairs at night.
retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - coat and braid.
detailed_description:
Cinematic, stormy night. [Shot 1] Ada climbs the spiral stairs with a lantern.
overall_soundscape:
Wind and waves against the stone tower.
non_diegetic_music:
N/A"""


@pytest.fixture
def no_llm(monkeypatch):
    """Every Calliope LLM entry point raises: client mode must never reach one."""

    async def boom(*_a, **_k):
        raise AssertionError("Calliope's LLM was called")

    class NoClient:
        def __init__(self, *a, **k):
            raise AssertionError("Calliope's LLM was called")

        @staticmethod
        def for_role(*_a, **_k):
            raise AssertionError("Calliope's LLM was called")

    for target in (
        "calliope.routers.story.generate_structured",
        "calliope.agent.script_agent.generate_structured",
        "calliope.agent.coverage_agent.generate_structured",
    ):
        monkeypatch.setattr(target, boom)
    for target in ("calliope.agent.continuity.LLMClient", "calliope.agent.video_agent.LLMClient"):
        monkeypatch.setattr(target, NoClient)
    monkeypatch.setattr(settings, "mcp_content_source", "client")


def _call(name: str, args: dict | None = None) -> tuple[dict, bool]:
    result = asyncio.run(mcp_server.call_tool(name, args or {}))
    return json.loads(result.content[0].text), bool(result.is_error)


def _project(client, target: str = "30 seconds") -> int:
    out, err = _call("create_project", {"title": "Lighthouse", "idea": "A keeper hears a bell", "target_duration": target})
    assert not err, out
    return out["project"]["id"]


def _story(n_beats: int) -> dict:
    return {
        "title": "The Last Bell",
        "logline": "A keeper follows a bell into the storm.",
        "characters": [{"name": "Ada", "role": "keeper", "appearance": "grey wool coat, silver braid"}],
        "locations": [{"name": "Lighthouse", "description": "White stone tower on a stormy cliff."}],
        "items": [{"name": "Lantern", "description": "Brass storm lantern."}],
        "beats": [{"title": f"Beat {i}", "description": f"Something happens {i}."} for i in range(1, n_beats + 1)],
    }


def test_story_brief_then_content_never_calls_llm(client, no_llm):
    pid = _project(client)
    brief, err = _call("generate_story")
    assert not err and brief["needs_content"] is True
    need = brief["brief"]["required_beats"]
    assert "beats" in brief["brief"]["system"].lower()

    short, err = _call("generate_story", {"content": _story(need - 1) if need > 1 else {"beats": []}})
    assert err  # validated before anything is written
    saved, err = _call("generate_story", {"content": _story(need)})
    assert not err, saved
    story = client.get(f"/api/projects/{pid}/story").json()
    assert len(story["beats"]) == need
    assert [c["name"] for c in story["characters"]] == ["Ada"]
    assert story["characters"][0]["consistency_prompt"]  # same seeding as a model draft


def test_script_with_clips_then_shots_and_plan(client, no_llm):
    pid = _project(client)
    need = _call("generate_story")[0]["brief"]["required_beats"]
    _call("generate_story", {"content": _story(need)})

    brief, err = _call("generate_script")
    assert not err and brief["needs_content"]
    required = brief["brief"]["required_scenes"]
    loc_id = brief["brief"]["locations"][0]["id"]
    char_id = brief["brief"]["characters"][0]["id"]
    scenes = []
    for i in range(1, required + 1):
        scene = {
            "heading": f"INT. LIGHTHOUSE - NIGHT {i}",
            "action": "Ada climbs the stairs.",
            "dialog": "ADA: Who rings at this hour?",
            "location_id": loc_id,
            "character_ids": [char_id],
        }
        if i == 1:
            scene["clips"] = [
                {"description": "Wide on the tower.", "shot_size": "wide", "duration_sec": 5},
                {"description": "Ada looks up.", "shot_size": "closeUp", "duration_sec": 4, "dialog_lines_covered": [1]},
            ]
        scenes.append(scene)
    out, err = _call("generate_script", {"content": {"scenes": scenes}})
    assert not err, out
    assert out["scenes_without_clips"] == list(range(2, required + 1))
    listed = client.get(f"/api/projects/{pid}/scenes").json()["scenes"]
    assert [len(s["clips"]) for s in listed][0] == 2

    if required > 1:
        shots, err = _call("break_into_shots", {"orders": [2]})
        assert not err and shots["needs_content"]
        assert shots["brief"]["scenes"][0]["order"] == 2
        done, err = _call(
            "break_into_shots",
            {"content": {"scenes": [{"order": 2, "clips": [
                {"description": "Ada at the window.", "shot_size": "medium", "duration_sec": 6, "dialog_lines_covered": [1]}
            ]}]}},
        )
        assert not err, done

    plan_brief, err = _call("set_continuity_plan")
    assert not err and plan_brief["needs_content"]
    clip_ids = plan_brief["brief"]["clip_ids"]
    saved, err = _call("set_continuity_plan", {"plan": {
        "overview": {"style": "Stormy night, cold blue"},
        "requirements": {"subjects": "Ada only"},
        "shots": [{"clip_id": cid, "action": "climb", "lighting": "lantern"} for cid in clip_ids],
    }})
    assert not err and saved["missing_clip_ids"] == []

    # A render changes the board basis; the client plan must survive it.
    from calliope.agent.continuity import ensure_continuity_plan
    from calliope.db import get_db

    conn = get_db(settings.db_path)
    conn.execute("UPDATE clips SET clip_path = 'rendered.mp4' WHERE id = ?", (clip_ids[0],))
    conn.commit()
    conn.close()
    kept = asyncio.run(ensure_continuity_plan(pid))  # llm=True path, still no LLM call
    assert kept["based_on"] == saved["based_on"] and kept["source"] == "client"
    assert kept["overview"]["style"] == "Stormy night, cold blue"


def test_prompts_brief_set_and_enqueue(client, no_llm):
    pid = _project(client)
    wf = client.post(
        "/api/workflows",
        json={"name": "r2v", "kind": "video", "workflow_json": json.loads(R2V_1REF.read_text(encoding="utf-8"))},
    ).json()
    assert wf["prompt_profile"] == "minimax_h3_ref"
    _call("add_location", {"name": "Lighthouse", "description": "White stone tower on a stormy cliff."})
    _call("add_character", {"name": "Ada", "appearance": "grey wool coat, silver braid"})
    _call("add_scene", {"heading": "INT. LIGHTHOUSE - NIGHT", "action": "Ada climbs.", "location_id": 1, "character_ids": [1]})
    clip, err = _call("add_clip", {"scene_id": 1, "description": "Ada climbs the stairs.", "duration_sec": 5, "workflow_id": wf["id"]})
    assert not err, clip
    clip_id = clip["clip"]["id"]

    refused, err = _call("enqueue_video_jobs", {"clip_ids": [clip_id]})
    assert err and refused["missing"]

    brief, err = _call("get_prompt_brief", {"clip_ids": [clip_id]})
    assert not err
    row = brief["clips"][0]
    assert row["needs_prompt"] and not row["draft_fresh"]
    assert "subject_definitions" in brief["system"]["minimax_h3_ref"]
    assert "White stone tower" in row["user"]  # the environment reaches the brief

    bad, err = _call("set_clip_prompts", {"prompts": [{"clip_id": clip_id, "prompt": "just a sentence"}]})
    assert err and bad["errors"]
    ok, err = _call("set_clip_prompts", {"prompts": [{"clip_id": clip_id, "prompt": H3_REF_PROMPT}]})
    assert not err and ok["saved"]
    assert _call("get_prompt_brief", {"clip_ids": [clip_id]})[0]["clips"][0]["draft_fresh"] is True

    # The UI's Review prompt shows the client draft at once: no LLM rewrite,
    # no continuity critic (every LLM entry point is trapped here).
    from calliope.agent.video_agent import preview_clip_prompt

    preview = asyncio.run(preview_clip_prompt(pid, clip_id, workflow_id=wf["id"]))
    assert preview["from_draft"] is True and preview["prompt"] == H3_REF_PROMPT
    assert preview["critic"] == {"ok": True, "notes": []}

    from calliope.queue.manager import queue_manager

    queue_manager.paused = True
    try:
        queued, err = _call("enqueue_video_jobs", {"clip_ids": [clip_id]})
        assert not err, queued
        payload = json.loads(queued["jobs"][0]["payload_json"])
        assert payload["prompt"] == H3_REF_PROMPT
    finally:
        queue_manager.paused = False


def test_calliope_mode_uses_the_llm_again(client, monkeypatch):
    monkeypatch.setattr(settings, "mcp_content_source", "calliope")
    called = []

    async def fake(*_a, **_k):
        called.append(1)
        raise RuntimeError("model offline")

    monkeypatch.setattr("calliope.routers.story.generate_structured", fake)
    _project(client)
    out, err = _call("generate_story")
    assert err and called  # went to the LLM instead of returning a brief
    assert "needs_content" not in out


def test_client_content_tools_are_mcp_only(client):
    """The in-app agent generates with Calliope's LLM: it never sees the
    hand-written prompt / plan tools."""
    from calliope.agent.harness import get_registry
    from calliope.agent.harness.registry import ToolContext

    registry = get_registry()
    chat = ToolContext(session_id=1, project_id=1, origin="chat")
    names = {t["function"]["name"] for t in registry.openai_payload(chat)}
    for tool in ("set_continuity_plan", "get_prompt_brief", "set_clip_prompts"):
        assert tool not in names
        out = asyncio.run(registry.execute(chat, tool, {}))
        assert out["ok"] is False and "MCP" in out["error"]
    assert {"set_continuity_plan", "get_prompt_brief", "set_clip_prompts"} <= {
        t.name for t in asyncio.run(mcp_server._list_tools(None, None)).tools
    }
