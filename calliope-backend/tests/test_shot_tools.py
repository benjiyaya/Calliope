"""Shot-builder plugin + /api/shots router tests (Build Scene).

The module-scoped autouse fixture redirects data_dir BEFORE any direct
get_db(settings.db_path) use — never write into the real calliope.db
(see clean-test-artifacts rule).
"""
from __future__ import annotations

import asyncio
import base64
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from calliope.config import settings
from calliope.db import get_db, migrate_db
from calliope.agent.harness.registry import ToolContext
from calliope.agent.harness.tools import execute_tool, openai_tools_payload
from calliope.agent.harness.orchestrator import orchestrate


@pytest.fixture(autouse=True, scope="module")
def _never_touch_real_db():
    prev = settings.data_dir
    with tempfile.TemporaryDirectory() as tmp:
        settings.data_dir = Path(tmp)
        settings.assets_dir = Path(tmp) / "assets"
        asyncio.run(migrate_db(settings.db_path))
        yield


def _mk_session() -> int:
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO agent_sessions (title, project_id) VALUES (?, NULL)",
            ("shot-test",),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _run(sid: int, tool: str, args: dict | None = None):
    from calliope.agent.harness.tools import TOOLS

    t = TOOLS[tool]
    return asyncio.run(t.executor(ToolContext(session_id=sid, project_id=None), args or {}))


def _png_data_url() -> str:
    # 1x1 transparent PNG
    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )
    return "data:image/png;base64," + base64.b64encode(raw).decode()


# ── scoping ────────────────────────────────────────────────────────────────


def test_shot_tools_hidden_in_linked_sessions():
    ctx = ToolContext(session_id=_mk_session(), project_id=42)
    names = {e["function"]["name"] for e in openai_tools_payload(ctx)}
    assert "get_scene" not in names
    assert "add_object" not in names
    assert "request_capture" not in names


def test_shot_tools_visible_in_sandbox():
    ctx = ToolContext(session_id=_mk_session(), project_id=None)
    names = {e["function"]["name"] for e in openai_tools_payload(ctx)}
    assert "get_scene" in names
    assert "set_joint" in names
    assert "set_shot" in names
    assert "request_capture" in names
    assert "add_keyframe" in names
    assert "update_keyframe" in names
    assert "delete_keyframe" in names
    assert "move_keyframe_time" in names
    assert "set_playback" in names


# ── get-or-create per session ──────────────────────────────────────────────


def test_get_scene_creates_one_composition_per_session():
    sid = _mk_session()
    out = _run(sid, "get_scene")
    assert out["ok"] is True
    comp_id = out["shot_id"]

    out2 = _run(sid, "get_scene")
    assert out2["shot_id"] == comp_id

    # A different session gets its own composition
    out3 = _run(_mk_session(), "get_scene")
    assert out3["shot_id"] != comp_id


# ── object CRUD ────────────────────────────────────────────────────────────


def test_add_and_delete_object_roundtrip():
    sid = _mk_session()
    added = _run(sid, "add_object", {"type": "male", "name": "Hero"})
    assert added["ok"] is True
    obj = added["object"]
    assert obj["type"] == "male"
    assert obj["name"] == "Hero"
    assert obj["transform"]["position"] == [0.0, 0, 0]

    scene = _run(sid, "get_scene")
    assert any(o["id"] == obj["id"] for o in scene["objects"])

    deleted = _run(sid, "delete_object", {"object_id": obj["id"]})
    assert deleted["ok"] is True
    scene = _run(sid, "get_scene")
    assert not any(o["id"] == obj["id"] for o in scene["objects"])


def test_add_object_rejects_unknown_type():
    sid = _mk_session()
    out = _run(sid, "add_object", {"type": "dragon"})
    assert out["ok"] is False
    assert "type must be one of" in out["error"]


def test_delete_unknown_object_errors():
    sid = _mk_session()
    out = _run(sid, "delete_object", {"object_id": "nope"})
    assert out["ok"] is False


# ── transforms ─────────────────────────────────────────────────────────────


def test_set_transform_partial_merge():
    sid = _mk_session()
    obj = _run(sid, "add_object", {"type": "cube"})["object"]
    out = _run(
        sid,
        "set_transform",
        {"object_id": obj["id"], "transform": {"position": [1.5, 0, -2]}},
    )
    assert out["ok"] is True
    assert out["object"]["transform"]["position"] == [1.5, 0.0, -2.0]
    assert out["object"]["transform"]["scale"] == [1, 1, 1]  # untouched

    bad = _run(
        sid,
        "set_transform",
        {"object_id": obj["id"], "transform": {"position": [1, 2]}},
    )
    assert bad["ok"] is False


def test_reset_transform():
    sid = _mk_session()
    obj = _run(sid, "add_object", {"type": "sphere"})["object"]
    _run(sid, "set_transform", {"object_id": obj["id"], "transform": {"position": [3, 3, 3]}})
    out = _run(sid, "reset_transform", {"object_id": obj["id"]})
    assert out["object"]["transform"]["position"] == [0.0, 0.0, 0.0]


# ── pose / joints ──────────────────────────────────────────────────────────


def test_set_joint_validates_lengths():
    sid = _mk_session()
    obj = _run(sid, "add_object", {"type": "female"})["object"]

    ok = _run(sid, "set_joint", {"object_id": obj["id"], "joint": "l_arm", "values": [10, 20, 30]})
    assert ok["ok"] is True

    knee = _run(sid, "set_joint", {"object_id": obj["id"], "joint": "l_knee", "values": [45]})
    assert knee["ok"] is True

    bad = _run(sid, "set_joint", {"object_id": obj["id"], "joint": "l_knee", "values": [1, 2, 3]})
    assert bad["ok"] is False
    assert "1 numbers" in bad["error"]

    unknown = _run(sid, "set_joint", {"object_id": obj["id"], "joint": "tail", "values": [0]})
    assert unknown["ok"] is False


def test_set_joint_rejects_camera():
    sid = _mk_session()
    obj = _run(sid, "add_object", {"type": "camera"})["object"]
    out = _run(sid, "set_joint", {"object_id": obj["id"], "joint": "head", "values": [0, 0, 0]})
    assert out["ok"] is False


def test_set_posture_rejects_malformed():
    sid = _mk_session()
    obj = _run(sid, "add_object", {"type": "male"})["object"]
    out = _run(
        sid,
        "set_posture",
        {"object_id": obj["id"], "posture": {"version": 7, "data": [[0, 0, 0]]}},
    )
    assert out["ok"] is False
    assert "26 entries" in out["error"]


def test_apply_pose_from_library():
    sid = _mk_session()
    obj = _run(sid, "add_object", {"type": "male"})["object"]
    poses = _run(sid, "list_poses")["poses"]
    assert len(poses) > 0
    out = _run(
        sid,
        "apply_pose",
        {"object_id": obj["id"], "pose_id": poses[0]["id"]},
    )
    assert out["ok"] is True
    assert out["object"]["posture"]["version"] == 7
    assert len(out["object"]["posture"]["data"]) == 26

    missing = _run(sid, "apply_pose", {"object_id": obj["id"], "pose_id": "authored.nope"})
    assert missing["ok"] is False

    reset = _run(sid, "reset_pose", {"object_id": obj["id"]})
    assert reset["object"]["posture"] is None


# ── shot framing ───────────────────────────────────────────────────────────


def test_set_shot_validates_and_merges():
    sid = _mk_session()
    out = _run(sid, "set_shot", {"shotSize": "mcu", "angle": "threeQuarterLeft"})
    assert out["ok"] is True
    assert out["shotParams"] == {"shotSize": "mcu", "angle": "threeQuarterLeft"}

    out = _run(sid, "set_shot", {"elevation": "low"})
    assert out["shotParams"]["shotSize"] == "mcu"  # merged
    assert out["shotParams"]["elevation"] == "low"

    bad = _run(sid, "set_shot", {"shotSize": "extreme"})
    assert bad["ok"] is False

    unknown_angle = _run(sid, "set_shot", {"angle": "dutch_45"})
    assert unknown_angle["ok"] is False


def test_set_shot_normalizes_legacy_ids():
    """Legacy '34_left'-era ids must map to the camelCase vocabulary the
    frontend solver indexes — an unnormalized '34_left' used to NaN the
    restored camera and blank the viewport after refresh."""
    sid = _mk_session()
    out = _run(sid, "set_shot", {"angle": "34_left", "composition": "left_third"})
    assert out["ok"] is True
    assert out["shotParams"]["angle"] == "threeQuarterLeft"
    assert out["shotParams"]["composition"] == "leftThird"


def test_preset_values_match_between_tools():
    sid = _mk_session()
    presets = _run(sid, "list_shot_presets")
    bad = _run(sid, "set_shot", {"composition": presets["composition"][0]})
    assert bad["ok"] is True


# ── keyframes / motion ─────────────────────────────────────────────────────


def _add_block(sid: int) -> str:
    out = _run(sid, "add_object", {"type": "cube"})
    return out["object"]["id"]


def test_add_keyframe_captures_current_transform():
    sid = _mk_session()
    oid = _add_block(sid)
    _run(sid, "set_transform", {"object_id": oid, "transform": {"position": [2, 0, 1]}})
    out = _run(sid, "add_keyframe", {"object_id": oid, "time": 1.5})
    assert out["ok"] is True
    kfs = out["object"]["keyframes"]
    assert len(kfs) == 2  # spawn keyframe at t=0 + this one
    assert kfs[-1]["time"] == 1.5
    assert kfs[-1]["transform"]["position"] == [2.0, 0.0, 1.0]


def test_set_transform_never_rewrites_committed_track():
    """Once a track exists, set_transform only stages the live fields —
    force-rewriting keyframes[0] snapped every earlier keyframe to the new
    position (a keyed walk-across teleported at t=0) and broke the
    stage→add_keyframe recipe."""
    sid = _mk_session()
    oid = _add_block(sid)
    _run(sid, "set_transform", {"object_id": oid, "transform": {"position": [4, 0, 0]}})
    _run(sid, "add_keyframe", {"object_id": oid, "time": 3.0})
    _run(sid, "set_transform", {"object_id": oid, "transform": {"position": [1, 0, 0]}})
    out = _run(sid, "get_scene", {})
    obj = next(o for o in out["objects"] if o["id"] == oid)
    assert obj["transform"]["position"] == [1.0, 0.0, 0.0]  # staged live
    assert obj["keyframes"][-1]["transform"]["position"] == [4.0, 0.0, 0.0]  # untouched
    assert obj["keyframes"][0]["transform"]["position"] == [0.0, 0.0, 0.0]  # spawn intact


def test_add_keyframe_replaces_near_time():
    sid = _mk_session()
    oid = _add_block(sid)
    _run(sid, "add_keyframe", {"object_id": oid, "time": 1.0})
    _run(sid, "set_transform", {"object_id": oid, "transform": {"position": [5, 0, 0]}})
    out = _run(sid, "add_keyframe", {"object_id": oid, "time": 1.01})  # within 1/24s
    assert out["ok"] is True
    kfs = out["object"]["keyframes"]
    assert len(kfs) == 2  # replaced, not appended
    assert kfs[-1]["time"] == 1.01
    assert kfs[-1]["transform"]["position"] == [5.0, 0.0, 0.0]


def test_add_keyframe_rejects_bad_time():
    sid = _mk_session()
    oid = _add_block(sid)
    out = _run(sid, "add_keyframe", {"object_id": oid, "time": 99})
    assert out["ok"] is False
    assert "between 0 and 60" in out["error"]


def test_update_delete_move_keyframe_roundtrip():
    sid = _mk_session()
    oid = _add_block(sid)
    added = _run(sid, "add_keyframe", {"object_id": oid, "time": 2.0})
    kf_id = added["object"]["keyframes"][-1]["id"]

    upd = _run(
        sid,
        "update_keyframe",
        {"object_id": oid, "keyframe_id": kf_id, "transform": {"position": [1, 2, 3]}},
    )
    assert upd["ok"] is True
    assert upd["object"]["keyframes"][-1]["transform"]["position"] == [1.0, 2.0, 3.0]

    moved = _run(sid, "move_keyframe_time", {"object_id": oid, "keyframe_id": kf_id, "time": 4.0})
    assert moved["ok"] is True
    assert moved["object"]["keyframes"][-1]["time"] == 4.0

    # With two anchors, deletion works…
    deleted = _run(sid, "delete_keyframe", {"object_id": oid, "keyframe_id": kf_id})
    assert deleted["ok"] is True
    assert len(deleted["object"]["keyframes"]) == 1

    # …but the last remaining keyframe is protected.
    spawn_kf = deleted["object"]["keyframes"][0]["id"]
    refuse = _run(sid, "delete_keyframe", {"object_id": oid, "keyframe_id": spawn_kf})
    assert refuse["ok"] is False
    assert "last keyframe" in refuse["error"]


def test_set_playback_updates_and_validates():
    sid = _mk_session()
    out = _run(sid, "set_playback", {"playing": True, "elapsed": 1.5, "speed": 2})
    assert out["ok"] is True
    assert out["playback"]["playing"] is True
    assert out["playback"]["elapsed"] == 1.5
    assert out["playback"]["speed"] == 2.0

    bad = _run(sid, "set_playback", {"duration": 120})
    assert bad["ok"] is False
    assert "1 and 60" in bad["error"]

    neg = _run(sid, "set_playback", {"elapsed": -1})
    assert neg["ok"] is False


# ── captures ───────────────────────────────────────────────────────────────


def test_capture_request_poll_and_fulfill(client, monkeypatch):
    sid = _mk_session()
    comp_id = _run(sid, "get_scene")["shot_id"]

    # request_capture asks the UI Three.js viewport to render the PNG: it
    # stamps capture_request_json, then polls until the router clears it and
    # a capture row lands (the UI POST /captures path, which this fulfills
    # directly in the DB below).

    # Fulfillment races the poll: post the capture in a background task
    import threading
    import time

    def _fulfill():
        time.sleep(1.0)
        c = get_db(settings.db_path)
        try:
            from fastapi.testclient import TestClient  # noqa: F401

            # Direct DB insert mirrors what the router does (POST is covered
            # separately); the poll only checks capture_request_json.
            c.execute(
                "INSERT INTO shot_capture (composition_id, kind, label, file_path, created_at) "
                "VALUES (?, 'image', 't', 'x.png', CURRENT_TIMESTAMP)",
                (comp_id,),
            )
            c.execute(
                "UPDATE shot_composition SET capture_request_json = NULL WHERE id = ?",
                (comp_id,),
            )
            c.commit()
        finally:
            c.close()

    threading.Thread(target=_fulfill, daemon=True).start()
    out = _run(sid, "request_capture", {"label": "pose ref"})
    assert out["ok"] is True
    assert out["file_path"] == "x.png"


def test_capture_timeout_is_failure_not_false_success(monkeypatch):
    """When the Build Scene page never lands the PNG, the tool must report
    failure — an ok:True with no file_path invited the agent to fabricate a
    reference path (and fed no signal to the fail-streak guard)."""
    from calliope.agent.harness.plugins import shot_builder

    monkeypatch.setattr(shot_builder, "CAPTURE_POLL_TIMEOUT_S", 0.2)
    sid = _mk_session()
    out = _run(sid, "request_capture", {"label": "nobody home"})
    assert out["ok"] is False
    assert out.get("capture_pending") is True
    assert "file_path" not in out
    assert "do NOT reference a file path" in out["error"]


def test_capture_upload_via_router(client):
    sid = _mk_session()
    comp_id = _run(sid, "get_scene")["shot_id"]
    resp = client.post(
        f"/api/shots/{comp_id}/captures",
        json={"data_url": _png_data_url(), "label": "ui capture"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "image"
    assert Path(body["file_path"]).exists()
    assert Path(body["file_path"]).resolve().is_relative_to(Path(settings.assets_dir).resolve())

    listing = client.get(f"/api/shots/{comp_id}/captures")
    assert listing.status_code == 200
    assert any(c["id"] == body["id"] for c in listing.json())

    # capture_request_json cleared
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT capture_request_json FROM shot_composition WHERE id = ?", (comp_id,)
        ).fetchone()
        assert row["capture_request_json"] is None
    finally:
        conn.close()


def test_capture_upload_rejects_garbage(client):
    sid = _mk_session()
    comp_id = _run(sid, "get_scene")["shot_id"]
    resp = client.post(
        f"/api/shots/{comp_id}/captures",
        json={"data_url": "data:image/png;base64,!!!not-base64!!!"},
    )
    assert resp.status_code == 422

    resp = client.post(
        f"/api/shots/{comp_id}/captures", json={"data_url": "hello world"}
    )
    assert resp.status_code == 422


# ── router CRUD + validation ───────────────────────────────────────────────


def test_router_scene_validation(client):
    sid = _mk_session()
    got = client.get(f"/api/shots/by-session/{sid}")
    assert got.status_code == 200
    comp = got.json()

    ok = client.patch(
        f"/api/shots/{comp['id']}",
        json={"scene_json": json.dumps({"objects": [{"id": "a", "type": "cube"}]})},
    )
    assert ok.status_code == 200

    bad = client.patch(
        f"/api/shots/{comp['id']}",
        json={"scene_json": json.dumps({"objects": [{"id": "a", "type": "dragon"}]})},
    )
    assert bad.status_code == 422

    bad = client.patch(
        f"/api/shots/{comp['id']}", json={"scene_json": "{not json"}
    )
    assert bad.status_code == 422

    # Title patch
    ok = client.patch(f"/api/shots/{comp['id']}", json={"title": "Duel at dawn"})
    assert ok.json()["title"] == "Duel at dawn"


def test_delete_capture(client):
    sid = _mk_session()
    comp_id = _run(sid, "get_scene")["shot_id"]
    body = client.post(
        f"/api/shots/{comp_id}/captures", json={"data_url": _png_data_url()}
    ).json()
    resp = client.delete(f"/api/shots/{comp_id}/captures/{body['id']}")
    assert resp.status_code == 200


# ── posture init (mannequin-vanish regression) ─────────────────────────────


def test_apply_joint_initializes_zeroed_entries():
    from calliope.agent.harness.plugins.shot_builder import _apply_joint, POSTURE_ENTRY_LENGTHS

    obj: dict = {"id": "m1", "type": "male"}
    _apply_joint(obj, "l_arm", [0.0, -0.5, 0.2])
    data = obj["posture"]["data"]
    assert len(data) == len(POSTURE_ENTRY_LENGTHS)
    for i, (entry, expected_len) in enumerate(zip(data, POSTURE_ENTRY_LENGTHS)):
        assert isinstance(entry, list)
        assert len(entry) == expected_len
        if i != 10:  # every untouched entry is properly zeroed, not empty
            assert all(v == 0.0 for v in entry)
    assert data[10] == [0.0, -0.5, 0.2]


def test_set_joint_no_empty_arrays_regression():
    # mannequin-js reads fixed-arity entries; an empty array NaNs the figure.
    sid = _mk_session()
    _run(sid, "get_scene")
    obj_id = _run(sid, "add_object", {"type": "female", "name": "Hero"})["object"]["id"]
    res = _run(sid, "set_joint", {"object_id": obj_id, "joint": "r_elbow", "values": [0.3]})
    assert res["ok"] is True
    scene_obj = _run(sid, "get_scene")["objects"]
    obj = next(o for o in scene_obj if o["id"] == obj_id)
    entries = obj["posture"]["data"]
    from calliope.agent.harness.plugins.shot_builder import POSTURE_ENTRY_LENGTHS

    assert all(len(e) == n for e, n in zip(entries, POSTURE_ENTRY_LENGTHS))
    assert not any(len(e) == 0 for e in entries)


# ── video captures ─────────────────────────────────────────────────────────


def _video_data_url() -> str:
    # Not a real codec payload — used where only the MIME + base64 matter
    # (the ffmpeg-missing 503 path never reaches the encoder).
    raw = b"\x1aE\xdf\xa3fake-ebml-header" + b"0" * 256
    return "data:video/webm;base64," + base64.b64encode(raw).decode()


def _mp4_data_url() -> str:
    # Fake MP4 header — the passthrough path stores it without ffmpeg.
    raw = b"\x00\x00\x00\x18ftypmp42" + b"0" * 256
    return "data:video/mp4;base64," + base64.b64encode(raw).decode()


def _mov_data_url() -> str:
    """A real 1-frame QuickTime clip (native mpeg4 codec, no external libs) —
    the transcode path needs a decodable video, unlike the fake-byte MIME
    checks above."""
    import subprocess

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "tiny.mov"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=duration=0.5:size=64x64:rate=10",
                "-c:v",
                "mpeg4",
                str(out),
            ],
            check=True,
            capture_output=True,
        )
        raw = out.read_bytes()
    return "data:video/quicktime;base64," + base64.b64encode(raw).decode()


def test_video_capture_mp4_passthrough(client):
    # Chromium MediaRecorder already emits MP4 — store it untouched (no ffmpeg).
    sid = _mk_session()
    comp_id = _run(sid, "get_scene")["shot_id"]
    resp = client.post(
        f"/api/shots/{comp_id}/captures",
        json={"data_url": _mp4_data_url(), "label": "camera export"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "video"
    assert Path(body["file_path"]).exists()
    assert Path(body["file_path"]).suffix == ".mp4"
    assert Path(body["file_path"]).resolve().is_relative_to(Path(settings.assets_dir).resolve())

    listing = client.get(f"/api/shots/{comp_id}/captures").json()
    mine = next(c for c in listing if c["id"] == body["id"])
    assert mine["kind"] == "video"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_video_capture_transcoded_to_mp4(client):
    # Firefox/WebM (or mov) must land in the picker as a real MP4.
    sid = _mk_session()
    comp_id = _run(sid, "get_scene")["shot_id"]
    resp = client.post(
        f"/api/shots/{comp_id}/captures",
        json={"data_url": _mov_data_url(), "label": "camera export"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "video"
    p = Path(body["file_path"])
    assert p.suffix == ".mp4"
    assert p.exists()
    # Real MP4: starts with an ftyp box, not a passthrough fake header.
    assert b"ftyp" in p.read_bytes()[:64]


def test_video_capture_webm_without_ffmpeg_503(client, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    sid = _mk_session()
    comp_id = _run(sid, "get_scene")["shot_id"]
    resp = client.post(
        f"/api/shots/{comp_id}/captures",
        json={"data_url": _video_data_url(), "label": "camera export"},
    )
    assert resp.status_code == 503


def test_capture_rejects_unknown_mime(client):
    sid = _mk_session()
    comp_id = _run(sid, "get_scene")["shot_id"]
    resp = client.post(
        f"/api/shots/{comp_id}/captures",
        json={"data_url": "data:application/pdf;base64," + base64.b64encode(b"%PDF").decode()},
    )
    assert resp.status_code == 422


# ── blind sessions never route through the planner ─────────────────────────


def test_blind_session_skips_planner(client):
    """Build Scene / sandbox turns go straight to the single loop: the planner
    would only misroute to project-scoped sub-agents (no shot tools there)."""
    import calliope.agent.harness.orchestrator as orch

    sid = _mk_session()

    class _SpyClient:
        instantiated = 0

        def __init__(self):
            type(self).instantiated += 1

        async def chat(self, *a, **kw):
            return json.dumps(
                {
                    "mode": "swarm",
                    "note": "Scheduling an assets sub-agent...",
                    "tasks": [{"role": "assets", "goal": "add blocks"}],
                }
            )

        async def close(self):
            return None

    async def fake_run_turn(ctx, history, *, on_message=None):
        return "single-loop answer"

    orig_client = orch.LLMClient
    orig_run_turn = orch.run_turn
    orch.LLMClient = _SpyClient
    orch.run_turn = fake_run_turn
    try:
        ctx = ToolContext(session_id=sid, project_id=None)
        out = asyncio.run(orchestrate(ctx, [], session_id=sid))
    finally:
        orch.LLMClient = orig_client
        orch.run_turn = orig_run_turn

    assert out == "single-loop answer"
    assert _SpyClient.instantiated == 0
