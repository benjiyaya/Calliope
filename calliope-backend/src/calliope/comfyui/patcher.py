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

    Sibling lookup is deliberately narrow — never a fuzzy match — so an unknown
    node's value lands on the one key we predicted rather than on a
    lookalike. Note that when the predicted key is absent from *every* pair we
    still return it unchanged, which does create a key the node never had;
    ``registry.PROMPT_CLASSES`` exists because that failure mode is silent.
    """
    if field in inputs:
        return field
    siblings = {"text": "value", "value": "text", "audio": "audio:", "audio:": "audio"}
    alt = siblings.get(field)
    if alt and alt in inputs:
        return alt
    return field


def bool_widget_key(inputs: dict[str, Any]) -> str | None:
    """The input key that holds a real JSON boolean, if this node has one.

    ``value`` wins when it is a bool (PrimitiveBoolean). Otherwise the sole
    bool key is used (a custom widget such as ``pe_enabled``). Several bool
    keys and no ``value`` is ambiguous, so it is left alone.
    """
    keys = [key for key, current in inputs.items() if isinstance(current, bool)]
    if "value" in keys:
        return "value"
    if len(keys) == 1:
        return keys[0]
    return None


_FALSE_WORDS = frozenset({"false", "0", "no", "off"})
_TRUE_WORDS = frozenset({"true", "1", "yes", "on"})


def coerce_bool(value: Any) -> bool | None:
    """Map a form value onto a JSON boolean. Never uses ``bool(str)``.

    The string ``\"false\"`` is non-empty, so ``bool(\"false\")`` is True and
    would leave a workflow default of true in place.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value == 0:
            return False
        if value == 1:
            return True
        return None
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _FALSE_WORDS:
            return False
        if word in _TRUE_WORDS:
            return True
    return None


def node_widget_field(node: dict[str, Any]) -> str:
    """The input key a node's user-facing widget patches onto.

    Used by strict mode to clear an exposed slot's baked-in default: it must
    resolve the same field ``patch_workflow`` would write.
    """
    inputs = node.get("inputs") or {}
    return _resolve_field(class_to_patch_field(node.get("class_type", "")), inputs)


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
        widget = bool_widget_key(inputs)
        if widget is not None:
            coerced = coerce_bool(value)
            # A non-bool written onto a boolean widget is what ComfyUI ignores,
            # falling back to the workflow default. Leave the widget alone.
            if coerced is None:
                continue
            inputs[widget] = coerced
        else:
            field = _resolve_field(class_to_patch_field(node.get("class_type", "")), inputs)
            inputs[field] = value
        node["inputs"] = inputs
        patched[key] = node
    return patched
