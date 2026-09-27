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


# ── environment / setting reaches every video prompt ──────────────────────

import asyncio  # noqa: E402

from calliope.agent import video_agent  # noqa: E402
from calliope.agent.prompts import (  # noqa: E402
    build_minimax_h3_ref_messages,
    minimax_h3_base_fallback,
    scene_video_prompt,
)

I2V_WORKFLOW = REPO_ROOT / "example_ComfyUI_workflows" / "minimax_h3-Turbo_Image2Video_20260813_API.json"
ONE_SLOT_WORKFLOW = REPO_ROOT / "example_ComfyUI_workflows" / "video_minimax_h3_r2v_API.json"

SHEET_PROMPT = (
    "CHARACTER SHEET — Elara\nLayout: single cinematic character reference sheet on a "
    "clean neutral backdrop, even studio lighting."
)
ELARA = {
    "id": 1,
    "name": "Elara",
    "appearance": "short-cropped dark hair, grey flight suit",
    "consistency_prompt": SHEET_PROMPT,
    "sheet_path": "elara.png",
}
VALLEY = {
    "name": "The Reclaimed Valley",
    "description": "A hidden oasis with dense green foliage and a sparkling river.",
    "consistency_prompt": "ENVIRONMENT REFERENCE — no people, no text.",
}
SCENE = {
    "heading": "EXT. RECLAIMED VALLEY - DAY",
    "action": "Elara steps out of the pod.",
    "duration_sec": 6,
}


class _DeadLLM:
    """LLMClient stand-in whose chat always fails → deterministic fallback."""

    def __init__(self, sink=None):
        self.sink = sink

    async def chat(self, messages, **kwargs):
        if self.sink is not None:
            self.sink.extend(messages)
        raise RuntimeError("offline")

    async def close(self):
        return None


def _stub_llm(monkeypatch, sink=None):
    monkeypatch.setattr(
        video_agent,
        "LLMClient",
        type("Stub", (), {"for_role": staticmethod(lambda role, **kw: _DeadLLM(sink))}),
    )


def test_one_slot_workflow_keeps_location_in_text(monkeypatch):
    """1 ref slot + 1 character + location: the image slot goes to the
    character, but the environment must still reach the prompt as text
    (it used to vanish — image AND description)."""
    inputs = parse_dynamic_inputs(_load(ONE_SLOT_WORKFLOW))
    subjects, paths, extra = video_agent._h3_subjects([ELARA], VALLEY, "valley.png", inputs)
    assert [s["kind"] for s in subjects] == ["character"]
    assert paths == ["elara.png"]
    assert extra == []

    sent: list[dict] = []
    _stub_llm(monkeypatch, sent)
    prompt, refs = asyncio.run(
        video_agent._build_prompt(
            "minimax_h3_ref", SCENE, [ELARA], VALLEY, "valley.png", inputs, timeout=1
        )
    )
    assert refs == ["elara.png"]
    user_msg = sent[-1]["content"]
    assert "The Reclaimed Valley" in user_msg and "sparkling river" in user_msg
    # Fallback (LLM offline) also carries the environment
    assert "sparkling river" in prompt
    assert "Natural ambience of The Reclaimed Valley" in prompt


def test_video_prompts_never_use_image_consistency_prompts(monkeypatch):
    """The sheet's image prompt ('neutral backdrop, studio lighting') and the
    location's ('no people') must not leak into video prompts."""
    inputs = parse_dynamic_inputs(_load(H3_WORKFLOW))  # 2 slots
    subjects, paths, _ = video_agent._h3_subjects([ELARA], VALLEY, "valley.png", inputs)
    assert [s["kind"] for s in subjects] == ["character", "location"]
    assert paths == ["elara.png", "valley.png"]
    assert subjects[0]["appearance"] == ELARA["appearance"]
    assert subjects[1]["appearance"] == VALLEY["description"]

    _stub_llm(monkeypatch)
    for profile in ("minimax_h3_ref", "minimax_h3_base", "prose"):
        prompt, _ = asyncio.run(
            video_agent._build_prompt(
                profile, SCENE, [ELARA], VALLEY, "valley.png", inputs, timeout=1
            )
        )
        assert "neutral backdrop" not in prompt, profile
        assert "no people" not in prompt, profile
        assert "sparkling river" in prompt, profile


def test_characters_without_slot_become_text_cast():
    inputs = parse_dynamic_inputs(_load(ONE_SLOT_WORKFLOW))
    kai = {"id": 2, "name": "Kai", "appearance": "red scarf", "sheet_path": "kai.png"}
    noimg = {"id": 3, "name": "Mo", "appearance": "tall, bald"}
    subjects, paths, extra = video_agent._h3_subjects([ELARA, kai, noimg], VALLEY, None, inputs)
    assert [s["name"] for s in subjects] == ["Elara"]
    assert [c["name"] for c in extra] == ["Kai", "Mo"]
    messages = build_minimax_h3_ref_messages(SCENE, subjects, extra_cast=extra)
    assert "- Kai: red scarf" in messages[1]["content"]


def test_detect_prompt_profile_h3_base():
    assert detect_prompt_profile(_load(I2V_WORKFLOW)) == "minimax_h3_base"
    assert (
        detect_prompt_profile(
            _load(REPO_ROOT / "example_ComfyUI_workflows" / "MiniMax-H3-Turbo-R2V-Extend-2Refs1Vid_API.json")
        )
        == "minimax_h3_ref"
    )


def test_h3_base_fallback_format():
    text = minimax_h3_base_fallback(
        {**SCENE, "dialog": "ELARA: We made it."},
        [{"name": "Elara", "appearance": "short-cropped dark hair"}],
        setting={"name": "The Reclaimed Valley", "description": "green foliage and a river"},
    )
    assert text.startswith("integrated_multimodal_description: [Shot 1] Cinematic, live-action")
    fields = ["integrated_multimodal_description:", "overall_soundscape:", "non_diegetic_music:"]
    assert [text.index(f) for f in fields] == sorted(text.index(f) for f in fields)
    assert "subject_definitions" not in text and "<Subject" not in text
    assert "The scene takes place in The Reclaimed Valley: green foliage and a river." in text
    assert "Elara: short-cropped dark hair." in text
    assert "<d>[English] We made it.</d>" in text


def test_prose_prompt_includes_location():
    text = scene_video_prompt(SCENE, [ELARA], VALLEY)
    assert "The scene takes place in The Reclaimed Valley" in text
    assert "Elara: short-cropped dark hair" in text
    assert "neutral backdrop" not in text
