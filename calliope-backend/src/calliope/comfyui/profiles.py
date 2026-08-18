"""Prompt profiles: how scene prompts are formatted for a workflow's model.

``prose`` (default) sends the classic flat prose paragraph. ``minimax_h3_ref``
rewrites the scene into MiniMax H3's six-section full-reference format at
video-enqueue time (see calliope.agent.prompts).
"""
from __future__ import annotations

from typing import Any

PROFILES = ("prose", "minimax_h3_ref")

DEFAULT_PROFILE = "prose"


def detect_prompt_profile(workflow_json: dict[str, Any]) -> str:
    """Suggest a prompt profile from the workflow's node classes.

    Deterministic class-type check (not title guessing): any MiniMaxH3* node
    (e.g. ``MiniMaxH3ReferenceToVideo``) means the workflow expects the H3
    full-reference prompt format.
    """
    for node in workflow_json.values():
        if not isinstance(node, dict):
            continue
        if str(node.get("class_type", "")).startswith("MiniMaxH3"):
            return "minimax_h3_ref"
    return DEFAULT_PROFILE
