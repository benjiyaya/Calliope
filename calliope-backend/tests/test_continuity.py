"""Project continuity ledger: stale refresh, binding H3 slice, critic fallback."""
from __future__ import annotations

import asyncio
import json

from calliope.agent.continuity import (
    continuity_lock_text,
    ensure_continuity_plan,
)
from calliope.agent.harness.orchestrator import ROLE_TOOLS
from calliope.config import settings
from calliope.db import get_db


def _mk_project(client, title: str) -> int:
    return client.post("/api/projects", json={"title": title, "tone": "noir"}).json()["id"]


def _add_scene(client, pid: int, order: int, **extra) -> dict:
    payload = {"order_index": order, "heading": f"S{order}", **extra}
    return client.post(f"/api/projects/{pid}/scenes", json=payload).json()


def _clip_id(pid: int, scene_id: int) -> int:
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT id FROM clips WHERE scene_id = ? AND project_id = ? "
            "ORDER BY order_index, id LIMIT 1",
            (scene_id, pid),
        ).fetchone()
        assert row is not None
        return int(row["id"])
    finally:
        conn.close()


def test_projects_have_continuity_column(client):
    conn = get_db(settings.db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
    finally:
        conn.close()
    assert "continuity_json" in cols


def test_plan_refreshes_on_script_change_and_skips_when_fresh(client, monkeypatch):
    pid = _mk_project(client, "Ledger")
    first = _add_scene(client, pid, 1, action="Maya opens the door.", dialog="MAYA: Come in.")
    second = _add_scene(client, pid, 2, action="She closes it.")
    first_clip = _clip_id(pid, first["id"])
    second_clip = _clip_id(pid, second["id"])
    conn = get_db(settings.db_path)
    try:
        conn.execute(
            "UPDATE clips SET description = ?, shot_size = ?, clip_path = ? WHERE id = ?",
            ("wide on the door", "wide", r"E:\assets\realized.mp4", first_clip),
        )
        conn.execute(
            "UPDATE clips SET description = ? WHERE id = ?",
            ("close on the latch", second_clip),
        )
        conn.commit()
    finally:
        conn.close()

    calls = {"n": 0, "saw_realized": False}

    class Scripted:
        def __init__(self, *args, **kwargs):
            pass

        @classmethod
        def for_role(cls, role, timeout=120.0):
            return cls()

        async def chat(self, messages, temperature=0.2, **kwargs):
            calls["n"] += 1
            text = messages[-1]["content"]
            calls["saw_realized"] = "REALIZED" in text
            return json.dumps(
                {
                    "overview": {"style": "noir", "lighting": "sodium"},
                    "requirements": {"lighting": "sodium lamps only", "dialogue": "Maya speaks"},
                    "shots": [
                        {
                            "clip_id": first_clip,
                            "lighting": "sodium",
                            "camera": "slow push",
                            "speakers": "MAYA: Come in.",
                        }
                    ],
                    "based_on": "ignore-me",
                }
            )

        async def close(self):
            return None

    monkeypatch.setattr("calliope.agent.continuity.LLMClient", Scripted)

    plan = asyncio.run(ensure_continuity_plan(pid))
    assert plan["refreshed"] is True
    assert calls["n"] == 1
    assert calls["saw_realized"] is True
    assert plan["based_on"]
    assert plan["based_on"] != "ignore-me"
    assert [s["clip_id"] for s in plan["shots"]] == [first_clip, second_clip]
    assert plan["shots"][0]["lighting"] == "sodium"
    # The model omitted clip 2; the stub keeps that shot.
    assert "latch" in plan["shots"][1]["action"]

    again = asyncio.run(ensure_continuity_plan(pid))
    assert again["refreshed"] is False
    assert again["based_on"] == plan["based_on"]
    assert calls["n"] == 1

    client.patch(
        f"/api/projects/{pid}/scenes/{first['id']}",
        json={"action": "Maya slams the door."},
    )
    refreshed = asyncio.run(ensure_continuity_plan(pid))
    assert refreshed["refreshed"] is True
    assert refreshed["based_on"] != plan["based_on"]
    assert calls["n"] == 2

    conn = get_db(settings.db_path)
    try:
        raw = conn.execute(
            "SELECT continuity_json FROM projects WHERE id = ?", (pid,)
        ).fetchone()["continuity_json"]
    finally:
        conn.close()
    stored = json.loads(raw)
    assert "refreshed" not in stored
    assert stored["based_on"] == refreshed["based_on"]


def test_dead_plan_model_returns_deterministic_plan(client, monkeypatch):
    pid = _mk_project(client, "Dead plan")
    scene = _add_scene(client, pid, 1, action="A rider crosses the flats.")
    clip_id = _clip_id(pid, scene["id"])

    class Dead:
        @classmethod
        def for_role(cls, role, timeout=120.0):
            return cls()

        async def chat(self, *args, **kwargs):
            raise RuntimeError("connection refused")

        async def close(self):
            return None

    monkeypatch.setattr("calliope.agent.continuity.LLMClient", Dead)
    plan = asyncio.run(ensure_continuity_plan(pid))
    assert plan["refreshed"] is True
    assert plan["shots"][0]["clip_id"] == clip_id
    assert "flats" in plan["shots"][0]["action"]
    assert plan["based_on"]


def test_h3_messages_bind_neighbor_shots_and_keep_file_slots():
    from calliope.agent.prompts import build_minimax_h3_ref_messages
    from calliope.agent.video_agent import resolve_h3_references
    from calliope.comfyui.parser import parse_dynamic_inputs

    plan = {
        "overview": {"style": "noir", "lighting": "sodium"},
        "requirements": {"lighting": "sodium lamps only"},
        "shots": [
            {
                "clip_id": 1,
                "window": "00:00.000-00:04.000",
                "action": "enter",
                "camera": "push in",
                "lighting": "dim sodium",
                "speakers": "MAYA: hello",
                "spatial": "screen left",
                "state_after": "door open",
            },
            {
                "clip_id": 2,
                "window": "00:04.000-00:08.000",
                "action": "turn",
                "camera": "hold",
                "lighting": "dim sodium",
                "speakers": "",
                "spatial": "screen left",
                "state_after": "",
            },
            {
                "clip_id": 3,
                "window": "00:08.000-00:12.000",
                "action": "leave",
                "camera": "pull out",
                "lighting": "brighter",
                "speakers": "",
                "spatial": "",
                "state_after": "hallway empty",
            },
        ],
    }
    lock = continuity_lock_text(plan, 2)
    assert "Previous shot" in lock
    assert "Next shot" in lock
    assert "Do not recast a subject" in lock
    assert "screen direction" in lock
    assert "enter" in lock and "leave" in lock

    scene = {"heading": "INT. HALL", "action": "She turns.", "duration_sec": 4, "dialog": ""}
    subjects = [
        {
            "index": 1,
            "kind": "reference",
            "name": "mercs",
            "appearance": "",
            "path": r"E:\assets\mercs.png",
        }
    ]
    messages = build_minimax_h3_ref_messages(scene, subjects, continuity=lock)
    text = messages[1]["content"]
    assert "Previous shot" in text
    assert "Next shot" in text
    assert "screen direction" in text
    assert "do not substitute another character or location" in text

    workflow = {
        "148": {
            "class_type": "LoadImage",
            "inputs": {"image": ""},
            "_meta": {"title": "Ref 1 (Input:image)"},
        },
        "149": {
            "class_type": "LoadImage",
            "inputs": {"image": ""},
            "_meta": {"title": "Ref 2 (Input:image)"},
        },
    }
    inputs = parse_dynamic_inputs(workflow)
    resolved, paths, _videos = resolve_h3_references(
        inputs,
        {"148": r"E:\assets\hero.png", "149": r"E:\assets\mercs.png"},
        [{"name": "Maya", "appearance": "olive jacket", "sheet_path": r"E:\assets\maya.png"}],
        {"name": "Metro Line", "description": "rain-slick rooftops"},
        r"E:\assets\metro.png",
    )
    assert resolved[1]["name"] == "mercs"
    assert "rooftop" not in (resolved[1].get("appearance") or "")
    assert paths[1].endswith("mercs.png")


def _insert_h3(conn, name: str) -> int:
    wf = {
        "10": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": ""},
            "_meta": {"title": "Main Prompt (Input:prompt)"},
        }
    }
    cur = conn.execute(
        """
        INSERT INTO workflows (name, kind, workflow_json, input_schema, output_schema,
                               prompt_profile, is_enabled)
        VALUES (?, 'video', ?, '[]', '[]', 'minimax_h3_ref', 1)
        """,
        (name, json.dumps(wf)),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_preview_returns_critic_notes(client, monkeypatch):
    from calliope.agent.video_agent import preview_clip_prompt

    pid = _mk_project(client, "Critic notes")
    scene = _add_scene(client, pid, 1, action="She turns.")
    clip_id = _clip_id(pid, scene["id"])
    conn = get_db(settings.db_path)
    try:
        wf_id = _insert_h3(conn, "H3 critic")
        conn.execute("UPDATE clips SET workflow_id = ? WHERE id = ?", (wf_id, clip_id))
        conn.commit()
    finally:
        conn.close()

    async def fake_plan(project_id, **kwargs):
        return {
            "based_on": "planhash",
            "overview": {"lighting": "sodium"},
            "requirements": {},
            "shots": [{"clip_id": clip_id, "action": "turn", "lighting": "sodium"}],
            "refreshed": True,
        }

    async def fake_rewrite(scene_, subjects, **kwargs):
        assert "sodium" in (kwargs.get("continuity") or "")
        return "COMPILED CANDIDATE"

    async def fake_critic(prompt, plan, clip_id_):
        assert prompt == "COMPILED CANDIDATE"
        return {"ok": False, "notes": ["lighting contradicts the previous shot"]}

    monkeypatch.setattr("calliope.agent.video_agent.ensure_continuity_plan", fake_plan)
    monkeypatch.setattr("calliope.agent.video_agent._h3_rewrite", fake_rewrite)
    monkeypatch.setattr("calliope.agent.video_agent.critique_prompt", fake_critic)

    result = asyncio.run(preview_clip_prompt(pid, clip_id))
    assert result["prompt"] == "COMPILED CANDIDATE"
    assert result["critic"]["ok"] is False
    assert result["critic"]["notes"] == ["lighting contradicts the previous shot"]


def test_failing_critic_falls_back_to_template(client, monkeypatch):
    from calliope.agent.video_agent import preview_clip_prompt

    pid = _mk_project(client, "Critic down")
    scene = _add_scene(client, pid, 1, action="A rider crosses the salt flats.")
    clip_id = _clip_id(pid, scene["id"])
    conn = get_db(settings.db_path)
    try:
        wf_id = _insert_h3(conn, "H3 critic down")
        conn.execute("UPDATE clips SET workflow_id = ? WHERE id = ?", (wf_id, clip_id))
        conn.commit()
    finally:
        conn.close()

    async def fake_plan(project_id, **kwargs):
        return {
            "based_on": "planhash",
            "overview": {},
            "requirements": {},
            "shots": [{"clip_id": clip_id, "action": "cross"}],
            "refreshed": False,
        }

    async def fake_rewrite(scene_, subjects, **kwargs):
        return "COMPILED CANDIDATE"

    async def fake_critic(prompt, plan, clip_id_):
        raise RuntimeError("judge down")

    monkeypatch.setattr("calliope.agent.video_agent.ensure_continuity_plan", fake_plan)
    monkeypatch.setattr("calliope.agent.video_agent._h3_rewrite", fake_rewrite)
    monkeypatch.setattr("calliope.agent.video_agent.critique_prompt", fake_critic)

    result = asyncio.run(preview_clip_prompt(pid, clip_id))
    assert result["prompt"].startswith("subject_definitions:")
    assert "salt flats" in result["prompt"]
    assert "COMPILED CANDIDATE" not in result["prompt"]
    assert result["critic"]["ok"] is False
    assert "judge down" in result["critic"]["notes"][0]


def test_failing_critic_keeps_a_saved_draft(client, monkeypatch):
    from calliope.agent.video_agent import _clip_prompt_hash, preview_clip_prompt

    pid = _mk_project(client, "Critic draft")
    scene = _add_scene(client, pid, 1, action="She waits.")
    clip_id = _clip_id(pid, scene["id"])
    conn = get_db(settings.db_path)
    try:
        wf_id = _insert_h3(conn, "H3 critic draft")
        row = conn.execute(
            """
            SELECT c.*, s.heading, s.action, s.dialog, s.location_id
            FROM clips c JOIN scenes s ON s.id = c.scene_id
            WHERE c.id = ?
            """,
            (clip_id,),
        ).fetchone()
        clip = dict(row)
        clip["character_ids"] = []
        based_on = _clip_prompt_hash(clip, ledger="planhash")
        conn.execute(
            "UPDATE clips SET workflow_id = ?, video_settings_json = ? WHERE id = ?",
            (
                wf_id,
                json.dumps(
                    {
                        "prompt_draft": "MY SAVED DRAFT",
                        "prompt_draft_meta": {"based_on": based_on},
                    }
                ),
                clip_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    async def fake_plan(project_id, **kwargs):
        return {
            "based_on": "planhash",
            "overview": {},
            "requirements": {},
            "shots": [],
            "refreshed": False,
        }

    async def fake_rewrite(scene_, subjects, **kwargs):
        raise AssertionError("fresh draft must skip the compiler")

    async def fake_critic(prompt, plan, clip_id_):
        raise RuntimeError("judge down")

    monkeypatch.setattr("calliope.agent.video_agent.ensure_continuity_plan", fake_plan)
    monkeypatch.setattr("calliope.agent.video_agent._h3_rewrite", fake_rewrite)
    monkeypatch.setattr("calliope.agent.video_agent.critique_prompt", fake_critic)

    result = asyncio.run(preview_clip_prompt(pid, clip_id))
    assert result["prompt"] == "MY SAVED DRAFT"
    assert result["from_draft"] is True
    assert result["critic"]["ok"] is False
    assert "judge down" in result["critic"]["notes"][0]


def test_refresh_tool_is_on_the_script_role(client, monkeypatch):
    from calliope.agent.harness.plugins.script import t_refresh_continuity_plan
    from calliope.agent.harness.registry import ToolContext

    assert "refresh_continuity_plan" in ROLE_TOOLS["script"]
    pid = _mk_project(client, "Tool")

    async def fake_ensure(project_id, force=False, live_refs=None):
        assert project_id == pid
        assert force is True
        return {"based_on": "abc", "shots": [{}, {}], "refreshed": True}

    monkeypatch.setattr("calliope.agent.continuity.ensure_continuity_plan", fake_ensure)
    result = asyncio.run(
        t_refresh_continuity_plan(ToolContext(session_id=1, project_id=pid), {})
    )
    assert result == {"ok": True, "based_on": "abc", "shots": 2, "refreshed": True}
