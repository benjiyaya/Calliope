"""Build Scene gates + the origin gate: a scene-origin session schedules
exactly one shot call, whatever the prose asks for."""

from __future__ import annotations

import asyncio

import pytest

from calliope.agent.capabilities import shot_ops
from calliope.agent.runtime.main_agent import schedule
from calliope.config import settings
from calliope.db import get_db, migrate_db


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "assets_dir", tmp_path / "assets")
    asyncio.run(migrate_db(settings.db_path))


def _mk_session(origin: str = "scene") -> int:
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO agent_sessions (title, project_id, origin) VALUES (?, NULL, ?)",
            ("gates", origin),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def test_gates_record_and_persist():
    sid = _mk_session()
    shot_ops.execute(sid, "get_scene", {})
    shot_ops.execute(
        sid, "record_build_scene_gate", {"name": "brief", "note": "locked the camera"}
    )
    shot_ops.execute(sid, "record_build_scene_gate", {"name": "cut"})
    gates = shot_ops.execute(sid, "get_build_scene_gates", {})["gates"]
    assert [g["name"] for g in gates] == ["brief", "cut"]
    assert gates[0]["note"] == "locked the camera"
    # Gates live on the persisted composition.
    fresh = shot_ops.execute(sid, "get_build_scene_gates", {})
    assert len(fresh["gates"]) == 2


def test_scene_origin_schedules_only_shot():
    plans = schedule("add a table, keyframe a walk, then render all scenes", origin="scene")
    assert [p.grant for p in plans] == ["shot"]
    plans = schedule("draft the story of a noir film", origin="scene")
    assert [p.grant for p in plans] == ["shot"]


def test_chat_origin_never_schedules_shot():
    plans = schedule("add a table and a lamp")
    assert "shot" not in [p.grant for p in plans]
