"""Patch user values into ComfyUI API-format workflow by nodeId."""
from __future__ import annotations

import copy
from typing import Any

from calliope.comfyui.registry import class_to_patch_field


def _resolve_field(field: str, inputs: dict[str, Any]) -> str:
    """Map the computed patch field onto a key that exists on this node.

    Known variants:
    - text ↔ value (PrimitiveString-style nodes expose `value`, not `text`).
    - audio ↔ audio: (VHS_LoadAudio names its widget `audio:` with a colon).
    The fallback stays guarded to these exact sibling pairs — never a fuzzy
    match — so an unknown node can't have its values written to some
    unrelated key ComfyUI would silently ignore.
    """
    if field in inputs:
        return field
    siblings = {"text": "value", "value": "text", "audio": "audio:", "audio:": "audio"}
    alt = siblings.get(field)
    if alt and alt in inputs:
        return alt
    return field


def patch_workflow(
    base: dict[str, Any],
    values_by_node_id: dict[str, Any],
) -> dict[str, Any]:
    patched = copy.deepcopy(base)
    for node_id, value in values_by_node_id.items():
        if value is None:
            continue
        key = str(node_id)
        node = patched.get(key)
        if not isinstance(node, dict):
            continue
        inputs = dict(node.get("inputs") or {})
        field = _resolve_field(class_to_patch_field(node.get("class_type", "")), inputs)
        inputs[field] = value
        node["inputs"] = inputs
        patched[key] = node
    return patched
