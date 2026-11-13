"""Session origin keeps the rails separate: origin is stamped at creation,
immutable via PATCH, and scene-origin rows stay out of the chat list."""

from __future__ import annotations

import asyncio

import pytest

from calliope.agent.harness.runner import Runner
from calliope.agent.runtime.main_agent import schedule
from calliope.config import settings
from calliope.db import get_db, migrate_db


def _origin(session_id: int) -> str:
    conn = get_db(settings.db_path)
    try:
        return conn.execute(
            "SELECT origin FROM agent_sessions WHERE id = ?", (session_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_create_session_stamps_origin(client):
    chat = client.post("/api/agent/sessions", json={"title": "chat"}).json()["id"]
    scene = client.post(
        "/api/agent/sessions", json={"title": "Scene · 7", "origin": "scene"}
    ).json()["id"]
    assert _origin(chat) == "chat"
    assert _origin(scene) == "scene"
    # Unknown origin rejected.
    assert (
        client.post("/api/agent/sessions", json={"origin": "canvas"}).status_code == 422
    )


def test_rename_never_moves_origin(client):
    scene = client.post(
        "/api/agent/sessions", json={"title": "Scene · 7", "origin": "scene"}
    ).json()["id"]
    client.patch(f"/api/agent/sessions/{scene}", json={"title": "Duel prep"})
    assert _origin(scene) == "scene"
    # origin is not patchable by shape — the payload ignores it.
    client.patch(f"/api/agent/sessions/{scene}", json={"title": "x", "origin": "chat"})
    assert _origin(scene) == "scene"


def test_session_list_excludes_scene_origin(client):
    chat = client.post("/api/agent/sessions", json={"title": "chat"}).json()["id"]
    scene = client.post(
        "/api/agent/sessions", json={"title": "Scene · 7", "origin": "scene"}
    ).json()["id"]
    default = client.get("/api/agent/sessions").json()
    ids = [s["id"] for s in default]
    assert chat in ids and scene not in ids
    scene_only = client.get("/api/agent/sessions", params={"origin": "scene"}).json()
    scene_ids = [s["id"] for s in scene_only]
    assert scene in scene_ids and chat not in scene_ids


def test_runner_scope_reads_origin(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "assets_dir", tmp_path / "assets")
    asyncio.run(migrate_db(settings.db_path))
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO agent_sessions (title, origin) VALUES ('shot', 'scene')"
        )
        sid = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    runner = Runner()
    project_id, origin = runner._scope(sid)
    assert project_id is None and origin == "scene"


def test_schedule_scene_origin_is_shot_only():
    assert [p.grant for p in schedule("render everything", origin="scene")] == ["shot"]
    assert [p.grant for p in schedule("write the script", origin="scene")] == ["shot"]
