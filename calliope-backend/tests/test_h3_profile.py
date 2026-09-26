"""Tests for the MiniMax H3 ref2video prompt profile."""
from __future__ import annotations

import json
from pathlib import Path

from calliope.agent.prompts import minimax_h3_ref_fallback
from calliope.comfyui.parser import parse_dynamic_inputs, parse_dynamic_outputs
from calliope.comfyui.profiles import detect_prompt_profile
from calliope.comfyui.smart_fill import ref_image_slots, smart_fill_inputs

REPO_ROOT = Path(__file__).resolve().parents[2]
H3_WORKFLOW = REPO_ROOT / "example_ComfyUI_workflows" / "video_minimax_h3_r2v_2ref_API.json"
KREA_WORKFLOW = REPO_ROOT / "example_ComfyUI_workflows" / "Krea2_t2i_20260818_API.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_h3_workflow_parses_expected_roles():
    workflow = _load(H3_WORKFLOW)
    inputs = parse_dynamic_inputs(workflow)
    by_node = {i["nodeId"]: i for i in inputs}

    assert by_node["138"]["role"] == "prompt"
    assert by_node["132"]["role"] == "duration"
    image_slots = [i["nodeId"] for i in ref_image_slots(inputs)]
    assert image_slots == ["148", "149"]  # node-id order = ref wiring order

    outputs = parse_dynamic_outputs(workflow)
    assert any(o["nodeId"] == "176" and o["kind"] == "video" for o in outputs)


def test_detect_prompt_profile():
    assert detect_prompt_profile(_load(H3_WORKFLOW)) == "minimax_h3_ref"
    assert detect_prompt_profile(_load(KREA_WORKFLOW)) == "prose"


def test_smart_fill_ref_images_and_duration():
    inputs = parse_dynamic_inputs(_load(H3_WORKFLOW))
    values = smart_fill_inputs(
        inputs,
        prompt="six-section h3 prompt",
        ref_images=["char.png", "loc.png", "surplus.png"],
        duration=8,
    )
    assert values["138"] == "six-section h3 prompt"
    assert values["148"] == "char.png"
    assert values["149"] == "loc.png"
    assert "surplus.png" not in values.values()
    assert values["132"] == 8


def test_smart_fill_ref_images_user_extra_wins():
    inputs = parse_dynamic_inputs(_load(H3_WORKFLOW))
    values = smart_fill_inputs(
        inputs,
        ref_images=["char.png"],
        extra={"148": "user-override.png"},
    )
    assert values["148"] == "user-override.png"


def test_h3_fallback_prompt_structure():
    scene = {
        "heading": "INT. COFFEE SHOP - DAY",
        "action": "Mia sits on the orange sofa, guarding a cookie.",
        "dialog": "MIA: Hey! Watch your dog!\nNARRATOR: Later that day.",
        "duration_sec": 6,
    }
    subjects = [
        {
            "index": 1,
            "kind": "character",
            "name": "Mia",
            "appearance": "long dark hair, blue cardigan",
        },
        {
            "index": 2,
            "kind": "location",
            "name": "Coffee Shop",
            "appearance": "exposed brick wall, neon sign",
        },
    ]
    text = minimax_h3_ref_fallback(scene, subjects)

    sections = [
        "subject_definitions:",
        "summary:",
        "retention_analysis:",
        "detailed_description:",
        "overall_soundscape:",
        "non_diegetic_music:",
    ]
    positions = [text.index(s) for s in sections]
    assert positions == sorted(positions), "sections must appear in the canonical order"

    assert '<Subject 1> is the character "Mia" in <Picture 1>' in text
    assert '<Subject 2> is the location "Coffee Shop" in <Picture 2>' in text
    assert text.count("fully_preserved") == 2
    assert "[reference generation]" in text
    assert "[Shot 1]" in text
    assert "<Subject 1> (S1) says, <d>[English] Hey! Watch your dog!</d>" in text
    # Narrator has no subject → stable voice description with its own speaker ID
    assert "Narrator (S2) says, <d>[English] Later that day.</d>" in text


def test_h3_fallback_without_subjects_or_dialog():
    scene = {"heading": "EXT. FOREST - NIGHT", "action": "Fog drifts between trees."}
    text = minimax_h3_ref_fallback(scene, [])
    assert "detailed_description:" in text
    assert "[Shot 1]" in text
    assert "<d>" not in text


def test_h3_fallback_includes_user_image_and_video():
    """A user image with no story text, plus a video, must both appear."""
    scene = {"heading": "INT. METRO - NIGHT", "action": "She swings the extinguisher."}
    subjects = [
        {"index": 1, "kind": "reference", "name": "mercs", "appearance": "", "path": "mercs.png"},
    ]
    videos = [{"index": 1, "path": "fight.mp4", "name": "fight.mp4"}]
    text = minimax_h3_ref_fallback(scene, subjects, videos)
    assert '<Subject 1> is the reference image in <Picture 1> ("mercs")' in text
    assert "<Video 1>" in text
    assert "motion_preserved" in text
    assert "Motion and camera follow <Video 1>." in text
    assert "Rooftops" not in text


def test_h3_messages_keep_video_and_picture_authority():
    from calliope.agent.prompts import build_minimax_h3_ref_messages

    scene = {"heading": "INT. METRO", "action": "She rises.", "duration_sec": 8}
    subjects = [
        {
            "index": 2,
            "kind": "reference",
            "name": "mercs",
            "appearance": "",
            "path": r"E:\assets\mercs.png",
        }
    ]
    videos = [{"index": 1, "name": "fight.mp4", "path": r"E:\assets\fight.mp4"}]
    messages = build_minimax_h3_ref_messages(
        scene,
        subjects,
        videos=videos,
        media_parts=[{"type": "text", "text": "Picture 2 is <Subject 2>."}],
    )
    user = messages[1]["content"]
    assert isinstance(user, list)
    text = user[0]["text"]
    assert "<Video 1>" in text
    assert "mercs.png" in text
    assert "file wins" in text
    assert user[1]["text"] == "Picture 2 is <Subject 2>."


def test_resolve_h3_references_prefers_form_files_over_story():
    """Slot files the user picked replace the story cast; empty slots keep it."""
    from calliope.agent.video_agent import resolve_h3_references
    from calliope.comfyui.parser import parse_dynamic_inputs

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
        "150": {
            "class_type": "LoadVideo",
            "inputs": {"video": ""},
            "_meta": {"title": "Motion (Input:video)"},
        },
    }
    inputs = parse_dynamic_inputs(workflow)
    characters = [
        {
            "name": "Maya",
            "appearance": "olive jacket",
            "sheet_path": r"E:\assets\maya.png",
        }
    ]
    location = {"name": "Metro Line", "description": "rain-slick rooftops"}
    loc_image = r"E:\assets\metro.png"

    empty_subjects, empty_paths, empty_videos = resolve_h3_references(
        inputs, {}, characters, location, loc_image
    )
    assert [s["name"] for s in empty_subjects] == ["Maya", "Metro Line"]
    assert empty_paths == [r"E:\assets\maya.png", r"E:\assets\metro.png"]
    assert empty_videos == []

    subjects, paths, videos = resolve_h3_references(
        inputs,
        {
            "148": r"E:\assets\maya.png",
            "149": r"E:\assets\mercs.png",
            "150": r"E:\assets\fight.mp4",
        },
        characters,
        location,
        loc_image,
    )
    assert subjects[0]["kind"] == "character"
    assert subjects[0]["name"] == "Maya"
    assert subjects[1]["kind"] == "reference"
    assert subjects[1]["name"] == "mercs"
    assert "rooftop" not in (subjects[1].get("appearance") or "")
    assert paths[1].endswith("mercs.png")
    assert videos[0]["index"] == 1
    assert videos[0]["path"].endswith("fight.mp4")


def test_reference_media_parts_labels_picture_and_video(monkeypatch, tmp_path):
    import asyncio

    from calliope.agent.video_agent import _reference_media_parts
    from calliope.config import settings

    monkeypatch.setattr(settings, "assets_dir", tmp_path)
    image = tmp_path / "mercs.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    video = tmp_path / "fight.mp4"
    video.write_bytes(b"0000")

    def fake_frames(path):
        assert Path(path).name == "fight.mp4"
        return [
            (0.5, "data:image/jpeg;base64,QQ=="),
            (1.5, "data:image/jpeg;base64,Qg=="),
        ]

    monkeypatch.setattr("calliope.agent.video_agent._video_attachment_frames", fake_frames)
    parts = asyncio.run(
        _reference_media_parts(
            [{"index": 1, "path": str(image)}],
            [{"index": 1, "path": str(video)}],
        )
    )
    texts = [p["text"] for p in parts if p.get("type") == "text"]
    assert any("Picture 1" in t and "<Subject 1>" in t for t in texts)
    assert any("<Video 1>" in t for t in texts)
    assert sum(1 for p in parts if p.get("type") == "image_url") == 3


def test_prompt_hash_changes_when_references_change():
    from calliope.agent.video_agent import _clip_prompt_hash

    clip = {"heading": "INT. METRO", "character_ids": []}
    bare = _clip_prompt_hash(clip)
    with_refs = _clip_prompt_hash(clip, references="img=mercs.png|vid=fight.mp4")
    assert bare != with_refs
    assert _clip_prompt_hash(clip) == bare


def test_h3_fallback_dialog_delivery_cue():
    scene = {
        "heading": "INT. HALL - NIGHT",
        "action": "Mia freezes.",
        "dialog": "MIA (whispering): Did you hear that?",
    }
    subjects = [
        {"index": 1, "kind": "character", "name": "Mia", "appearance": "chestnut ponytail"},
    ]
    text = minimax_h3_ref_fallback(scene, subjects)
    # Cue kept as delivery direction; speaker still matched to the subject
    assert "<Subject 1> (S1) says whispering, <d>[English] Did you hear that?</d>" in text
