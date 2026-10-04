from __future__ import annotations

import copy

from calliope.comfyui.patcher import patch_workflow
from calliope.queue.worker import _strip_leaky_defaults


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
            "inputs": {"ref_audios.ref_audio_0": ["153", 0]},
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


def test_strict_strips_baked_defaults_of_exposed_slots():
    tpl = _template()
    out = _strip_leaky_defaults(copy.deepcopy(tpl), tpl, SCHEMA, {})
    # Exposed prompt/reference slots must not inherit the workflow's defaults.
    assert out["9900"]["inputs"]["value"] == ""
    assert out["153"]["inputs"]["audio"] == ""
    assert out["149"]["inputs"]["image"] == ""
    # A non-leaky setting (duration) keeps its default.
    assert out["135"]["inputs"]["value"] == 3.0


def test_strict_keeps_values_that_differ_from_the_default():
    tpl = _template()
    provided = {"153": "calliope/my.wav", "9900": "real prompt", "149": "calliope/ref.png"}
    patched = patch_workflow(tpl, provided)
    out = _strip_leaky_defaults(patched, tpl, SCHEMA, provided)
    assert out["153"]["inputs"]["audio"] == "calliope/my.wav"
    assert out["9900"]["inputs"]["value"] == "real prompt"
    assert out["149"]["inputs"]["image"] == "calliope/ref.png"


def test_strict_strips_smart_fill_default_injection():
    """smart-fill copies the template default into input_values; strict must
    treat that as 'not a user choice' and clear it, or the default leaks."""
    tpl = _template()
    provided = {"153": "tf2-sfx.mp3"}
    patched = patch_workflow(tpl, provided)
    out = _strip_leaky_defaults(patched, tpl, SCHEMA, provided)
    assert out["153"]["inputs"]["audio"] == ""


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
