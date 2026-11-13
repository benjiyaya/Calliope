"""Build Scene shot tools: JSON-CRUD on shot_composition.scene_json, the real
pose library with arity-validated postures, keyframe track ownership, and the
60s playback cap. The composition is per session (origin 'scene')."""

from __future__ import annotations

import asyncio

import pytest

from calliope.agent.capabilities import shot_ops
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
            ("shot-test", origin),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _run(sid: int, tool: str, args: dict | None = None) -> dict:
    return shot_ops.execute(sid, tool, args or {})


def test_get_scene_creates_one_composition_per_session():
    sid = _mk_session()
    first = _run(sid, "get_scene")["scene"]
    again = _run(sid, "get_scene")["scene"]
    other = _run(_mk_session(), "get_scene")["scene"]
    assert first == again
    assert other is not first
    assert first["objects"] == []
    assert first["shotParams"]["angle"] == "wide"


def test_add_rename_delete_object_roundtrip():
    sid = _mk_session()
    _run(sid, "get_scene")
    added = _run(sid, "add_object", {"id": "hero", "name": "Hero", "kind": "block"})
    assert added["ok"] is True
    scene = _run(sid, "get_scene")["scene"]
    assert scene["objects"][0]["id"] == "hero"
    # Duplicate id rejected.
    assert _run(sid, "add_object", {"id": "hero"})["ok"] is False
    renamed = _run(sid, "rename_object", {"object_id": "hero", "name": "Mara"})
    assert renamed["scene"]["objects"][0]["name"] == "Mara"
    deleted = _run(sid, "delete_object", {"object_id": "hero"})
    assert deleted["scene"]["objects"] == []


def test_object_scoped_tools_need_an_existing_object():
    sid = _mk_session()
    _run(sid, "get_scene")
    assert _run(sid, "delete_object", {"object_id": "nope"})["ok"] is False
    assert "object_id is required" in _run(sid, "set_transform", {"object_id": "nope"})["error"]


def test_transform_stage_and_reset():
    sid = _mk_session()
    _run(sid, "get_scene")
    _run(sid, "add_object", {"id": "box"})
    staged = _run(
        sid, "set_transform", {"object_id": "box", "transform": {"position": [1, 2, 3]}}
    )
    obj = staged["scene"]["objects"][0]
    assert obj["transform"]["position"] == [1, 2, 3]
    reset = _run(sid, "reset_transform", {"object_id": "box"})
    assert reset["scene"]["objects"][0]["transform"] == {"position": [0, 0, 0]}
    selected = _run(sid, "select_object", {"object_id": "box"})
    assert selected["scene"]["selectedObjectId"] == "box"


def test_clear_scene():
    sid = _mk_session()
    _run(sid, "get_scene")
    _run(sid, "add_object", {"id": "a"})
    _run(sid, "add_object", {"id": "b"})
    cleared = _run(sid, "clear_scene")
    assert cleared["scene"]["objects"] == []
    assert cleared["scene"]["selectedObjectId"] is None


def test_set_joint_requires_joint_and_values():
    sid = _mk_session()
    _run(sid, "get_scene")
    _run(sid, "add_object", {"id": "figure"})
    assert _run(sid, "set_joint", {"object_id": "figure"})["ok"] is False
    assert _run(sid, "set_joint", {"object_id": "figure", "joint": "elbow_l", "values": []})["ok"] is False
    out = _run(
        sid, "set_joint", {"object_id": "figure", "joint": "elbow_l", "values": [10, 0, 0]}
    )
    assert out["scene"]["objects"][0]["posture"]["elbow_l"] == [10, 0, 0]


def test_set_posture_requires_dict():
    sid = _mk_session()
    _run(sid, "get_scene")
    _run(sid, "add_object", {"id": "figure"})
    assert _run(sid, "set_posture", {"object_id": "figure", "posture": "sitting"})["ok"] is False


def test_pose_library_is_real_and_arity_checked():
    sid = _mk_session()
    _run(sid, "get_scene")
    _run(sid, "add_object", {"id": "figure"})
    poses = _run(sid, "list_poses")["poses"]
    # The authored library (calliope/poses.py + user overrides), not a stub.
    assert len(poses) >= 10
    assert all({"id", "name", "category", "source"} <= set(p) for p in poses)
    applied = _run(
        sid, "apply_pose", {"object_id": "figure", "pose_id": "authored.sitting"}
    )
    posture = applied["scene"]["objects"][0]["posture"]
    assert posture["version"] == 7
    # mannequin-js reads entries positionally: every entry must be non-empty.
    assert len(posture["data"]) == 26
    assert all(isinstance(entry, list) and entry for entry in posture["data"])
    missing = _run(sid, "apply_pose", {"object_id": "figure", "pose_id": "authored.nope"})
    assert missing["ok"] is False
    assert "list_poses" in missing["error"]
    reset = _run(sid, "reset_pose", {"object_id": "figure"})
    assert reset["scene"]["objects"][0]["posture"] == {}


def test_set_shot_and_presets():
    sid = _mk_session()
    _run(sid, "get_scene")
    presets = _run(sid, "list_shot_presets")["presets"]
    assert "wide" in presets and "threeQuarterLeft" in presets
    out = _run(sid, "set_shot", {"angle": "medium", "composition": "ruleOfThirds"})
    assert out["scene"]["shotParams"] == {"angle": "medium", "composition": "ruleOfThirds"}


def test_keyframe_track_ownership():
    """Stage (set_transform) → add_keyframe captures; a later stage never
    rewrites committed keyframes."""
    sid = _mk_session()
    _run(sid, "get_scene")
    _run(sid, "add_object", {"id": "walker"})
    _run(sid, "set_transform", {"object_id": "walker", "transform": {"position": [0, 0, 0]}})
    _run(sid, "add_keyframe", {"object_id": "walker", "time": 0})
    _run(sid, "set_transform", {"object_id": "walker", "transform": {"position": [5, 0, 0]}})
    _run(sid, "add_keyframe", {"object_id": "walker", "time": 2})
    scene = _run(sid, "get_scene")["scene"]
    track = scene["objects"][0]["keyframes"]
    assert len(track) == 2
    assert track[0]["transform"]["position"] == [0, 0, 0]
    assert track[1]["transform"]["position"] == [5, 0, 0]
    # Staging again does NOT touch the committed track.
    _run(sid, "set_transform", {"object_id": "walker", "transform": {"position": [9, 9, 9]}})
    track = _run(sid, "get_scene")["scene"]["objects"][0]["keyframes"]
    assert track[0]["transform"]["position"] == [0, 0, 0]
    # Update + move + delete with last-keyframe protection.
    kf_id = track[1]["id"]
    _run(sid, "update_keyframe", {"object_id": "walker", "keyframe_id": kf_id, "time": 3})
    moved = _run(sid, "move_keyframe_time", {"object_id": "walker", "keyframe_id": kf_id, "time": 4})
    assert moved["scene"]["objects"][0]["keyframes"][1]["time"] == 4
    assert _run(
        sid, "delete_keyframe", {"object_id": "walker", "keyframe_id": track[0]["id"]}
    )["ok"] is True
    last = _run(sid, "get_scene")["scene"]["objects"][0]["keyframes"]
    assert _run(
        sid, "delete_keyframe", {"object_id": "walker", "keyframe_id": last[0]["id"]}
    )["ok"] is False
    assert "The last keyframe stays" in _run(
        sid, "delete_keyframe", {"object_id": "walker", "keyframe_id": last[0]["id"]}
    )["error"]
    assert _run(sid, "update_keyframe", {"object_id": "walker", "keyframe_id": "ghost"})["ok"] is False


def test_playback_duration_capped_at_60():
    sid = _mk_session()
    _run(sid, "get_scene")
    out = _run(sid, "set_playback", {"duration": 120, "playing": True})
    assert out["scene"]["playback"]["duration"] == 60.0
    out = _run(sid, "set_playback", {"duration": 0.5})
    assert out["scene"]["playback"]["duration"] == 1.0


def test_capture_request_and_gates():
    sid = _mk_session()
    _run(sid, "get_scene")
    capture = _run(sid, "request_capture", {"kind": "image"})
    assert capture["scene"]["captureRequest"] == {"kind": "image"}
    _run(sid, "record_build_scene_gate", {"name": "brief", "note": "locked the camera"})
    gates = _run(sid, "get_build_scene_gates")["gates"]
    assert gates == [{"name": "brief", "note": "locked the camera"}]


def test_unknown_tool_names_error():
    sid = _mk_session()
    _run(sid, "get_scene")
    assert _run(sid, "run_workflow")["ok"] is False
    assert shot_ops.execute(_mk_session(), "get_scene", {})["ok"] is True
