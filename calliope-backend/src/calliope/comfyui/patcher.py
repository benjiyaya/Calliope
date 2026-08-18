"""Patch user values into ComfyUI API-format workflow by nodeId."""
from __future__ import annotations

import copy
from typing import Any

from calliope.comfyui.registry import class_to_patch_field


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
        field = class_to_patch_field(node.get("class_type", ""))
        if field == "text" and "text" not in inputs and "value" in inputs:
            field = "value"
        if field == "value" and "text" in inputs and "value" not in inputs:
            field = "text"
        inputs[field] = value
        node["inputs"] = inputs
        patched[key] = node
    return patched
