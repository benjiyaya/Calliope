"""Session-origin contract: Build Scene's per-scene sessions stay out of the
AI Canvas sidebar. origin is stamped at creation and never drifts via rename.
Scene-origin sessions are also scoped to the shot_* blockout tools only."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from calliope.config import settings
from calliope.db import get_db, migrate_db
from calliope.agent.harness.registry import (
    GUARD_SCENE_TOOL_SCOPE,
    ToolContext,
)


@pytest.fixture(autouse=True, scope="module")
def _never_touch_real_db():
    """Tests in this file call get_db(settings.db_path) directly (the
    backfill test has no client-only path) — redirect data_dir to a temp dir
    for the whole module so the real calliope.db can never be written."""
    prev = settings.data_dir
    with tempfile.TemporaryDirectory() as tmp:
        settings.data_dir = Path(tmp)
        asyncio.run(migrate_db(settings.db_path))
        yield
    settings.data_dir = prev


def test_default_sessions_exclude_scene_origin(client):
    chat = client.post("/api/agent/sessions", json={"title": "My chat"}).json()
    scene = client.post("/api/agent/sessions", json={"title": "Scene · 999", "origin": "scene"}).json()
    listed = client.get("/api/agent/sessions").json()
    ids = {s["id"] for s in listed}
    assert chat["id"] in ids
    assert scene["id"] not in ids
    assert all(s.get("origin") != "scene" for s in listed)


def test_origin_filter_returns_scene_sessions(client):
    scene = client.post("/api/agent/sessions", json={"origin": "scene"}).json()
    client.post("/api/agent/sessions", json={"title": "chat"})
    listed = client.get("/api/agent/sessions?origin=scene").json()
    ids = {s["id"] for s in listed}
    assert scene["id"] in ids
    assert all(s["origin"] == "scene" for s in listed)


def test_origin_defaults_to_chat(client):
    s = client.post("/api/agent/sessions", json={}).json()
    assert s["origin"] == "chat"


def test_origin_rejects_unknown_value(client):
    r = client.post("/api/agent/sessions", json={"origin": "plotter"})
    assert r.status_code == 422


def test_rename_does_not_change_origin(client):
    scene = client.post("/api/agent/sessions", json={"origin": "scene"}).json()
    r = client.patch(f"/api/agent/sessions/{scene['id']}", json={"title": "Totally a chat"})
    assert r.status_code == 200
    listed = client.get("/api/agent/sessions").json()
    assert scene["id"] not in {s["id"] for s in listed}
    assert r.json()["title"] == "Totally a chat"


def test_backfill_matches_legacy_scene_titled_rows(client):
    """The one-time backfill (run inside migrate_db when the origin column is
    added) stamps origin='scene' on exactly the legacy rows titled
    'Scene · N' and nothing else. The migration branch itself is gated on the
    ALTER (runs once), so this asserts the statement's row-matching
    invariant directly on legacy-shaped data."""
    conn = get_db(settings.db_path)
    try:
        legacy_ids = []
        for title in ("Scene · 100", "Scene · 200"):
            cur = conn.execute(
                "INSERT INTO agent_sessions (title, origin) VALUES (?, 'chat')", (title,)
            )
            legacy_ids.append(cur.lastrowid)
        plain = conn.execute(
            "INSERT INTO agent_sessions (title, origin) VALUES ('my chat', 'chat')"
        ).lastrowid
        # Scene-titled but already scene (idempotency); a title that merely
        # contains the prefix without starting with it is NOT backfilled.
        already = conn.execute(
            "INSERT INTO agent_sessions (title, origin) VALUES ('Scene · 300', 'scene')"
        ).lastrowid
        tricky = conn.execute(
            "INSERT INTO agent_sessions (title, origin) VALUES ('my Scene · chat', 'chat')"
        ).lastrowid
        conn.commit()

        conn.execute("UPDATE agent_sessions SET origin = 'scene' WHERE title LIKE 'Scene · %'")
        conn.commit()

        for sid in legacy_ids + [already]:
            row = conn.execute("SELECT origin FROM agent_sessions WHERE id = ?", (sid,)).fetchone()
            assert row["origin"] == "scene", sid
        for sid in (plain, tricky):
            row = conn.execute("SELECT origin FROM agent_sessions WHERE id = ?", (sid,)).fetchone()
            assert row["origin"] == "chat", sid

        for sid in (*legacy_ids, plain, already, tricky):
            conn.execute("DELETE FROM agent_sessions WHERE id = ?", (sid,))
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────
# Build Scene tool scope: scene-origin sessions see shot tools + base ONLY
# ─────────────────────────────────────────────────────────────────────────


def _scene_ctx() -> ToolContext:
    return ToolContext(session_id=1, project_id=None, origin="scene")


def test_scene_payload_only_allowlisted_tools(client):
    from calliope.agent.harness.tools import openai_tools_payload

    names = {entry["function"]["name"] for entry in openai_tools_payload(_scene_ctx())}
    assert "get_scene" in names and "add_object" in names and "add_keyframe" in names
    assert "run_workflow" not in names
    assert "enqueue_asset_jobs" not in names
    assert "generate_story" not in names
    assert "create_canvas_node" not in names
    assert "ask_user" in names and "read_skill" in names


def test_chat_payload_unaffected_by_scene_scope(client):
    from calliope.agent.harness.tools import openai_tools_payload

    chat_ctx = ToolContext(session_id=1, project_id=None, origin="chat")
    names = {t["function"]["name"] for t in openai_tools_payload(chat_ctx)}
    # A chat sandbox still has its generation tools (render visibility is
    # governed by user_allows_render, not origin).
    assert "run_workflow" in names or "list_workflows" in names


def test_scene_execute_denies_out_of_scope_tool(client):
    from calliope.agent.harness.tools import execute_tool

    result = asyncio.run(execute_tool(_scene_ctx(), "run_workflow", {"workflow_id": 1, "prompt": "x"}))
    assert result["ok"] is False
    assert result.get("reason_code") == GUARD_SCENE_TOOL_SCOPE


def test_scene_execute_still_runs_shot_tool(client):
    from calliope.agent.harness.tools import execute_tool

    result = asyncio.run(execute_tool(_scene_ctx(), "get_scene", {}))
    # The shot tool executes (empty scene is fine) — not scope-blocked.
    assert result.get("ok") is True or "reason_code" not in result


# ─────────────────────────────────────────────────────────────────────────
# Startup recovery: sessions orphaned in 'running' by a killed process
# ─────────────────────────────────────────────────────────────────────────


def test_recover_orphaned_running_sessions(client):
    """A backend restart leaves no live tasks, so every 'running' row at
    startup is orphaned (the loop died before writing turn/end) and must be
    reset to 'idle' — otherwise the UI shows a phantom Working… state with a
    dead Stop button."""
    from calliope.agent.harness.runner import runner

    conn = get_db(settings.db_path)
    try:
        stuck = conn.execute(
            "INSERT INTO agent_sessions (title, status) VALUES ('stuck', 'running')"
        ).lastrowid
        fine = conn.execute(
            "INSERT INTO agent_sessions (title, status) VALUES ('idle-one', 'idle')"
        ).lastrowid
        errored = conn.execute(
            "INSERT INTO agent_sessions (title, status) VALUES ('err-one', 'error')"
        ).lastrowid
        conn.commit()

        assert runner.recover_orphaned_sessions() >= 1

        for sid, expected in ((stuck, "idle"), (fine, "idle"), (errored, "error")):
            row = conn.execute(
                "SELECT status FROM agent_sessions WHERE id = ?", (sid,)
            ).fetchone()
            assert row["status"] == expected, sid
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────
# Build Scene harness prompts (Video Scene Build guardrails)
# ─────────────────────────────────────────────────────────────────────────


def test_scene_system_prompt_bans_comfy_and_enforces_gates(client):
    """origin=scene prompt must reinforce Build Scene policy in harness
    (not only SKILL.md): shot_* only, no ComfyUI, Brief/Cut, recipes."""
    from calliope.agent.harness import get_prompts

    prompt = asyncio.run(get_prompts().assemble(_scene_ctx()))
    assert "BUILD SCENE POLICY" in prompt
    assert "Three.js viewport" in prompt
    assert "ComfyUI" in prompt or "comfy_*" in prompt
    assert "run_workflow" in prompt
    assert "Brief" in prompt or "brief" in prompt
    assert "Cut" in prompt or "cut" in prompt
    assert "per-frame" in prompt or "recipes" in prompt.lower()
    # Scene mode must not push sandbox Comfy generation paths
    assert "GENERATING IN SANDBOX" not in prompt


def test_chat_system_prompt_skips_build_scene_policy(client):
    from calliope.agent.harness import get_prompts

    chat_ctx = ToolContext(session_id=1, project_id=None, origin="chat")
    prompt = asyncio.run(get_prompts().assemble(chat_ctx))
    assert "BUILD SCENE POLICY" not in prompt
