from __future__ import annotations

import copy

from calliope.comfyui.patcher import patch_workflow
from calliope.queue.worker import _apply_strict_mode


def _template():
    return {
        "153": {
            "class_type": "LoadAudio",
            "inputs": {"audio": "tf2-sfx.mp3"},
            "_meta": {"title": "(Input:audio) Ref Audio 1"},
        },
        "156": {
            "class_type": "LoadVideo",
            "inputs": {"file": "", "video-preview": ""},
            "_meta": {"title": "(Input:video) Ref Video 1"},
        },
        "152": {
            "class_type": "GetVideoComponents",
            "inputs": {"video": ["156", 0]},
            "_meta": {"title": "Get Video Components"},
        },
        "149": {
            "class_type": "LoadImage",
            "inputs": {"image": "baked.png"},
            "_meta": {"title": "(Input:image) Ref Image 1"},
        },
        "9900": {
            "class_type": "PrimitiveStringMultiline",
            "inputs": {"value": "baked prompt"},
            "_meta": {"title": "(Input:prompt) Prompt"},
        },
        "135": {
            "class_type": "PrimitiveFloat",
            "inputs": {"value": 3.0},
            "_meta": {"title": "(Input:duration) Duration"},
        },
        "145": {
            "class_type": "MiniMaxH3ReferenceToVideo",
            "inputs": {
                "ref_images.ref_image_0": ["149", 0],
                "ref_videos.ref_video_0": ["152", 0],
                "ref_video_audios.ref_video_audio_0": ["152", 1],
                "ref_audios.ref_audio_0": ["153", 0],
                "prompt": ["9900", 0],
            },
            "_meta": {"title": "H3"},
        },
    }


SCHEMA = [
    {"nodeId": "153", "role": "audio", "kind": "audio"},
    {"nodeId": "156", "role": "video", "kind": "video"},
    {"nodeId": "149", "role": "image", "kind": "image"},
    {"nodeId": "9900", "role": "prompt", "kind": "textarea"},
    {"nodeId": "135", "role": "duration", "kind": "number"},
]


def test_strict_prunes_baked_media_and_blanks_baked_prompt():
    tpl = _template()
    out = _apply_strict_mode(copy.deepcopy(tpl), tpl, SCHEMA, {})
    # Workflow-file media references are pruned (node + wiring, cascading
    # through GetVideoComponents) so nothing bakes into the render.
    assert "153" not in out
    assert "149" not in out
    assert "156" not in out
    assert "152" not in out
    h3 = out["145"]["inputs"]
    assert "ref_images.ref_image_0" not in h3
    assert "ref_videos.ref_video_0" not in h3
    assert "ref_video_audios.ref_video_audio_0" not in h3
    assert "ref_audios.ref_audio_0" not in h3
    # A baked prompt is blanked (not pruned) so its encoder survives.
    assert out["9900"]["inputs"]["value"] == ""
    assert h3["prompt"] == ["9900", 0]
    # Non-leaky settings are untouched.
    assert out["135"]["inputs"]["value"] == 3.0


def test_strict_keeps_user_supplied_values():
    tpl = _template()
    provided = {
        "153": "calliope/my.wav",
        "149": "calliope/ref.png",
        "156": "calliope/prev.mp4",
        "9900": "llm prompt",
    }
    patched = patch_workflow(tpl, provided)
    out = _apply_strict_mode(patched, tpl, SCHEMA, provided)
    assert out["153"]["inputs"]["audio"] == "calliope/my.wav"
    assert out["149"]["inputs"]["image"] == "calliope/ref.png"
    assert out["156"]["inputs"]["file"] == "calliope/prev.mp4"
    assert out["9900"]["inputs"]["value"] == "llm prompt"
    assert out["145"]["inputs"]["ref_audios.ref_audio_0"] == ["153", 0]


def test_strict_prunes_smart_fill_default_injection():
    """smart-fill re-injects the template default; strict treats a value equal
    to the file's own value as 'not a user choice' and prunes it."""
    tpl = _template()
    provided = {"153": "tf2-sfx.mp3"}
    patched = patch_workflow(tpl, provided)
    out = _apply_strict_mode(patched, tpl, SCHEMA, provided)
    assert "153" not in out


def test_workflow_create_defaults_strict_mode_on(client):
    r = client.post(
        "/api/workflows",
        json={
            "name": "Strict default",
            "kind": "image",
            "workflow_json": {
                "10": {
                    "class_type": "LoadImage",
                    "inputs": {"image": ""},
                    "_meta": {"title": "Ref (Input:image)"},
                }
            },
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["strict_mode"] is True

    wid = data["id"]
    r = client.patch(f"/api/workflows/{wid}", json={"strict_mode": False})
    assert r.status_code == 200, r.text
    assert r.json()["strict_mode"] is False
