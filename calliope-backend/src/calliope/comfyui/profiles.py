"""Prompt profiles: how scene prompts are formatted for a workflow's model.

``prose`` (default) sends the classic flat prose paragraph. ``minimax_h3_ref``
rewrites the scene into MiniMax H3's six-section full-reference format at
video-enqueue time (see calliope.agent.prompts). The rewrite reads the clip
form's ``(Input:image)`` and ``(Input:video)`` files; story images only fill
slots the user left empty. ``minimax_h3_base`` rewrites it into H3's base
multi-shot (text-to-video) format for checkpoints without reference-image
slots.
"""
from __future__ import annotations

from typing import Any

PROFILES = ("prose", "minimax_h3_ref", "minimax_h3_base")

H3_PROFILES = frozenset({"minimax_h3_ref", "minimax_h3_base"})

DEFAULT_PROFILE = "prose"

# Node classes that consume reference images → the six-section ref format.
_H3_REF_CLASSES = ("MiniMaxH3ReferenceToVideo", "MiniMaxH3VideoExtend")


def detect_prompt_profile(workflow_json: dict[str, Any]) -> str:
    """Suggest a prompt profile from the workflow's node classes.

    Deterministic class-type check (not title guessing): a reference-to-video
    or video-extend node means the H3 full-reference format; any other
    MiniMaxH3* node (e.g. ``MiniMaxH3ImageToVideo``, the base t2v/i2v
    checkpoint) means the base multi-shot format.
    """
    classes = [
        str(node.get("class_type", ""))
        for node in workflow_json.values()
        if isinstance(node, dict)
    ]
    if any(c.startswith(_H3_REF_CLASSES) for c in classes):
        return "minimax_h3_ref"
    if any(c.startswith("MiniMaxH3") for c in classes):
        return "minimax_h3_base"
    return DEFAULT_PROFILE
