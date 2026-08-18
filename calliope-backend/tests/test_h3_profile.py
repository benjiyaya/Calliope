"""Tests for the MiniMax H3 ref2video prompt profile."""
from __future__ import annotations

import json
from pathlib import Path

from calliope.agent.prompts import minimax_h3_ref_fallback
from calliope.comfyui.parser import parse_dynamic_inputs, parse_dynamic_outputs
from calliope.comfyui.profiles import detect_prompt_profile
from calliope.comfyui.smart_fill import ref_image_slots, smart_fill_inputs

REPO_ROOT = Path(__file__).resolve().parents[2]
H3_WORKFLOW = REPO_ROOT / "ComfyAPI" / "video_minimax_h3_r2v_API.json"
KREA_WORKFLOW = REPO_ROOT / "ComfyAPI" / "krea2_t2i_2pass-nsfwEnable-API.json"


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
    assert any(o["nodeId"] == "153" and o["kind"] == "video" for o in outputs)


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
