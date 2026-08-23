"""H3 Motion Context pair detect, prefix rewrite, duration bump."""
from __future__ import annotations

from calliope.comfyui.motion_context import (
    apply_motion_context,
    classify_motion_role,
    continued_duration,
    motion_prefix,
)
from calliope.comfyui.registry import class_to_input_kind, class_to_patch_field


def test_load_video_kind_and_patch_field():
    assert class_to_input_kind("LoadVideo") == "video"
    assert class_to_patch_field("LoadVideo") == "file"
    assert class_to_patch_field("VHS_LoadVideo") == "video"


def test_classify_first_vs_next():
    first = {
        "274": {"class_type": "MiniMaxH3MotionContextSaveLatent", "inputs": {}},
        "272": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {}},
    }
    nxt = {
        "274": {"class_type": "MiniMaxH3MotionContextSaveLatent", "inputs": {}},
        "275": {"class_type": "MiniMaxH3MotionContext", "inputs": {"context_latent": ["277", 0]}},
        "277": {"class_type": "MiniMaxH3MotionContextLoadLatent", "inputs": {}},
        "276": {"class_type": "MiniMaxH3MotionContextTrim", "inputs": {}},
    }
    plain = {"3": {"class_type": "SaveVideo", "inputs": {}}}
    assert classify_motion_role(first) == "first"
    assert classify_motion_role(nxt) == "next"
    assert classify_motion_role(plain) is None


def test_apply_prefix_and_unwire():
    wf = {
        "274": {
            "class_type": "MiniMaxH3MotionContextSaveLatent",
            "inputs": {"filename_prefix": "h3_context/clip", "clip_index": 2},
        },
        "277": {
            "class_type": "MiniMaxH3MotionContextLoadLatent",
            "inputs": {"latent_path": "h3_context", "clip_index": 1},
        },
        "275": {
            "class_type": "MiniMaxH3MotionContext",
            "inputs": {"context_latent": ["277", 0], "conditioning": ["272", 0]},
        },
    }
    save_prefix, load_folder = motion_prefix(12)
    started = apply_motion_context(wf, project_id=12, continue_motion=False)
    assert started["274"]["inputs"]["filename_prefix"] == save_prefix
    assert started["277"]["inputs"]["latent_path"] == load_folder
    assert "context_latent" not in started["275"]["inputs"]

    chained = apply_motion_context(
        {
            "275": {
                "class_type": "MiniMaxH3MotionContext",
                "inputs": {"context_latent": ["277", 0]},
            },
        },
        project_id=12,
        continue_motion=True,
    )
    assert chained["275"]["inputs"]["context_latent"] == ["277", 0]


def test_continued_duration():
    assert continued_duration(5) == 5 + 22 / 24
    assert continued_duration(None) is None


def test_prepare_media_inputs_uploads_video(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock

    from calliope.comfyui.client import ComfyUIClient

    video = tmp_path / "walk.mp4"
    video.write_bytes(b"fake-mp4")
    client = ComfyUIClient(base_url="http://comfy.test")
    client.upload_video = AsyncMock(return_value="calliope/walk.mp4")
    wf = {
        "10": {
            "class_type": "LoadVideo",
            "inputs": {"file": str(video)},
        }
    }
    try:
        out = asyncio.run(client.prepare_media_inputs(wf))
        assert out["10"]["inputs"]["file"] == "calliope/walk.mp4"
        client.upload_video.assert_awaited_once()
    finally:
        asyncio.run(client.close())


FIRST_MOTION = {
    "274": {
        "class_type": "MiniMaxH3MotionContextSaveLatent",
        "inputs": {"filename_prefix": "h3_context/clip"},
    },
    "209": {
        "class_type": "PrimitiveStringMultiline",
        "inputs": {"value": ""},
        "_meta": {"title": "Prompt (Input:prompt)"},
    },
    "5": {
        "class_type": "PrimitiveFloat",
        "inputs": {"value": 5},
        "_meta": {"title": "(Input:duration) Duration"},
    },
    "282": {
        "class_type": "PrimitiveInt",
        "inputs": {"value": 1},
        "_meta": {"title": "(input:clipindex) Save Motion Context Clip Index"},
    },
}

NEXT_MOTION = {
    **FIRST_MOTION,
    "275": {
        "class_type": "MiniMaxH3MotionContext",
        "inputs": {"context_latent": ["277", 0]},
    },
    "277": {"class_type": "MiniMaxH3MotionContextLoadLatent", "inputs": {}},
    "276": {"class_type": "MiniMaxH3MotionContextTrim", "inputs": {}},
    "281": {
        "class_type": "PrimitiveInt",
        "inputs": {"value": 1},
        "_meta": {"title": "(input:clipindex) Load Motion Context Clip Index"},
    },
}


def test_analyze_and_enqueue_picks_first_then_next(client):
    first_a = client.post("/api/workflows/analyze", json={"workflow_json": FIRST_MOTION})
    next_a = client.post("/api/workflows/analyze", json={"workflow_json": NEXT_MOTION})
    assert first_a.status_code == 200
    assert first_a.json()["motion_role"] == "first"
    assert next_a.status_code == 200
    assert next_a.json()["motion_role"] == "next"

    first = client.post(
        "/api/workflows",
        json={
            "name": "FirstMotion",
            "kind": "video",
            "workflow_json": FIRST_MOTION,
            "prompt_profile": "prose",
        },
    ).json()
    nxt = client.post(
        "/api/workflows",
        json={
            "name": "NextMotion",
            "kind": "video",
            "workflow_json": NEXT_MOTION,
            "prompt_profile": "prose",
        },
    ).json()
    assert first["motion_role"] == "first"
    assert nxt["motion_role"] == "next"

    pid = client.post("/api/projects", json={"title": "Hallway"}).json()["id"]
    s1 = client.post(
        f"/api/projects/{pid}/scenes",
        json={"heading": "Walk", "order_index": 1, "duration_sec": 5},
    ).json()
    s2 = client.post(
        f"/api/projects/{pid}/scenes",
        json={"heading": "Door", "order_index": 2, "duration_sec": 5},
    ).json()

    client.post("/api/jobs/pause")
    try:
        r = client.post(
            f"/api/jobs/projects/{pid}/generate-videos",
            json={"scene_ids": [s1["id"], s2["id"]]},
        )
        assert r.status_code == 200
        jobs = r.json()["jobs"]
        assert len(jobs) == 2
        j1, j2 = jobs
        assert j1["workflow_id"] == first["id"]
        assert j1["payload"]["continue_motion"] is False
        assert j1["payload"]["input_values"]["282"] == 1
        assert j1["payload"]["input_values"]["5"] == 5

        assert j2["workflow_id"] == nxt["id"]
        assert j2["payload"]["continue_motion"] is True
        assert j2["payload"]["input_values"]["281"] == 1
        assert j2["payload"]["input_values"]["282"] == 2
        assert j2["payload"]["input_values"]["5"] == 5 + 22 / 24
    finally:
        client.post("/api/jobs/resume")


def test_continue_motion_fails_without_previous_clip(client):
    client.post(
        "/api/workflows",
        json={
            "name": "FirstMotion",
            "kind": "video",
            "workflow_json": FIRST_MOTION,
            "prompt_profile": "prose",
        },
    )
    client.post(
        "/api/workflows",
        json={
            "name": "NextMotion",
            "kind": "video",
            "workflow_json": NEXT_MOTION,
            "prompt_profile": "prose",
        },
    )
    pid = client.post("/api/projects", json={"title": "No prev"}).json()["id"]
    client.post(
        f"/api/projects/{pid}/scenes",
        json={"heading": "One", "order_index": 1, "duration_sec": 5},
    )
    s2 = client.post(
        f"/api/projects/{pid}/scenes",
        json={"heading": "Two", "order_index": 2, "duration_sec": 5},
    ).json()

    client.post("/api/jobs/pause")
    try:
        r = client.post(
            f"/api/jobs/projects/{pid}/generate-videos",
            json={"scene_ids": [s2["id"]], "continue_motion": True},
        )
        assert r.status_code == 400
        assert "previous clip" in r.json()["detail"].lower()
    finally:
        client.post("/api/jobs/resume")
