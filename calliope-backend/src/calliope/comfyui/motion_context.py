"""H3 Motion Context pairing: FirstMotion vs NextMotion, prefix isolation."""
from __future__ import annotations

from typing import Any, Literal

MotionRole = Literal["first", "next"]

CLASS_SAVE = "MiniMaxH3MotionContextSaveLatent"
CLASS_LOAD = "MiniMaxH3MotionContextLoadLatent"
CLASS_CONTEXT = "MiniMaxH3MotionContext"
CLASS_TRIM = "MiniMaxH3MotionContextTrim"

# H3 native rate. context_length 22 frames ≈ 0.92s removed by Trim on NextMotion.
CONTEXT_LENGTH = 22
CONTEXT_FPS = 24.0


def _class_types(workflow: dict[str, Any]) -> set[str]:
    return {
        str(node.get("class_type") or "")
        for node in workflow.values()
        if isinstance(node, dict)
    }


def classify_motion_role(workflow: dict[str, Any]) -> MotionRole | None:
    """``first`` / ``next`` from live H3 Motion Context class_types, else None."""
    classes = _class_types(workflow)
    has_save = CLASS_SAVE in classes
    has_load = CLASS_LOAD in classes
    has_ctx = CLASS_CONTEXT in classes
    if has_load and has_ctx:
        return "next"
    if has_save and not has_load:
        return "first"
    return None


def motion_prefix(project_id: int) -> tuple[str, str]:
    """(save filename_prefix, load latent_path folder) under Comfy output/."""
    folder = f"calliope/p{int(project_id)}"
    return f"{folder}/clip", folder


def apply_motion_context(
    workflow: dict[str, Any],
    *,
    project_id: int,
    continue_motion: bool,
) -> dict[str, Any]:
    """Rewrite Save/Load paths onto a per-project folder.

    When ``continue_motion`` is false, drop ``context_latent`` so a NextMotion
    graph can run as a chain start (Load is then unreachable).
    """
    save_prefix, load_folder = motion_prefix(project_id)
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type") or ""
        inputs = dict(node.get("inputs") or {})
        if class_type == CLASS_SAVE:
            inputs["filename_prefix"] = save_prefix
            node["inputs"] = inputs
        elif class_type == CLASS_LOAD:
            inputs["latent_path"] = load_folder
            node["inputs"] = inputs
        elif class_type == CLASS_CONTEXT and not continue_motion:
            inputs.pop("context_latent", None)
            node["inputs"] = inputs
    return workflow


def continued_duration(duration_sec: int | float | None) -> float | None:
    """Add the Trim head so delivered length matches the scene duration."""
    if duration_sec is None:
        return None
    return float(duration_sec) + (CONTEXT_LENGTH / CONTEXT_FPS)
