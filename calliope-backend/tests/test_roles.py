"""Tests for Comfy title role tags and smart-fill by role."""
from __future__ import annotations

from calliope.comfyui.parser import parse_dynamic_inputs
from calliope.comfyui.roles import parse_title_tag
from calliope.comfyui.smart_fill import smart_fill_inputs


def test_parse_title_tag_with_role():
    kind, role, label = parse_title_tag("主提示词 (Input:prompt)")
    assert kind == "input"
    assert role == "prompt"
    assert "主提示词" in label
    assert "(Input" not in label


def test_parse_title_tag_role_only():
    kind, role, label = parse_title_tag("(Input:prompt)")
    assert kind == "input"
    assert role == "prompt"
    assert label == "prompt"


def test_parse_title_tag_plain_input():
    kind, role, label = parse_title_tag("Width (Input)")
    assert kind == "input"
    assert role is None
    assert label == "Width"


def test_smart_fill_uses_role_not_node_id():
    workflow = {
        "999": {
            "inputs": {"value": ""},
            "class_type": "PrimitiveStringMultiline",
            "_meta": {"title": "Whatever Name (Input:prompt)"},
        },
        "7": {
            "inputs": {"value": 512},
            "class_type": "PrimitiveInt",
            "_meta": {"title": "W (Input:width)"},
        },
    }
    inputs = parse_dynamic_inputs(workflow)
    values = smart_fill_inputs(
        inputs,
        prompt="Jake tall athletic",
        extra={"999": "", "7": 1280},
    )
    assert values["999"] == "Jake tall athletic"
    assert values["7"] == 1280


def test_input_prompt_alias_positive():
    workflow = {
        "3": {
            "inputs": {"text": ""},
            "class_type": "CLIPTextEncode",
            "_meta": {"title": "Pos (Input:positive)"},
        },
    }
    inputs = parse_dynamic_inputs(workflow)
    assert inputs[0]["role"] == "prompt"
    values = smart_fill_inputs(inputs, prompt="hello")
    assert values["3"] == "hello"


def test_smart_fill_extra_overrides_context():
    """Form/user values must win on regenerate so edits are not wiped."""
    workflow = {
        "9": {
            "inputs": {"value": ""},
            "class_type": "PrimitiveStringMultiline",
            "_meta": {"title": "Global (Input:prompt)"},
        },
        "12": {
            "inputs": {"image": ""},
            "class_type": "LoadImage",
            "_meta": {"title": "Ref 1 (Input:character)"},
        },
    }
    inputs = parse_dynamic_inputs(workflow)
    values = smart_fill_inputs(
        inputs,
        prompt="auto from scene",
        character_image="/old/sheet.png",
        extra={"9": "user edited prompt", "12": "/new/ref.png"},
    )
    assert values["9"] == "user edited prompt"
    assert values["12"] == "/new/ref.png"


def test_input_duration_alias():
    workflow = {
        "5": {
            "inputs": {"value": 10},
            "class_type": "PrimitiveFloat",
            "_meta": {"title": "(input:duration) Duration"},
        },
    }
    inputs = parse_dynamic_inputs(workflow)
    assert inputs[0]["role"] == "duration"
    values = smart_fill_inputs(inputs, duration=7)
    assert values["5"] == 7


def test_video_and_audio_roles():
    workflow = {
        "10": {
            "inputs": {"file": ""},
            "class_type": "LoadVideo",
            "_meta": {"title": "Clip (Input:video)"},
        },
        "11": {
            "inputs": {"audio": ""},
            "class_type": "LoadAudio",
            "_meta": {"title": "Bed (Input:audio)"},
        },
    }
    inputs = parse_dynamic_inputs(workflow)
    by_role = {i["role"]: i for i in inputs}
    assert by_role["video"]["kind"] == "video"
    assert by_role["audio"]["kind"] == "audio"
    values = smart_fill_inputs(
        inputs,
        ref_videos=["/tmp/walk.mp4"],
        ref_audios=["/tmp/bed.mp3"],
    )
    assert values["10"] == "/tmp/walk.mp4"
    assert values["11"] == "/tmp/bed.mp3"
