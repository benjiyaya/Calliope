"""Smart-fill ComfyUI dynamic inputs from project/scene context via role tags."""
from __future__ import annotations

from typing import Any

from calliope.comfyui.roles import input_has_role, normalize_input_role


def _find_by_role(inputs: list[dict[str, Any]], *roles: str) -> dict[str, Any] | None:
    for inp in inputs:
        if input_has_role(inp, *roles):
            return inp
    return None


def _find_all_by_role(inputs: list[dict[str, Any]], *roles: str) -> list[dict[str, Any]]:
    """All inputs matching any of the roles, sorted by numeric node id.

    Node-id order is the documented ref-slot order: for multi-reference
    workflows (e.g. MiniMax H3 ref2video), ``(Input:image)`` slots are filled
    in this order and it defines ``<Subject N>`` numbering in the prompt.
    """
    matches = [inp for inp in inputs if input_has_role(inp, *roles)]
    return sorted(matches, key=lambda i: int(i["nodeId"]))


def ref_image_slots(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Generic ``(Input:image)`` ref slots in documented fill order (node id).

    For multi-reference workflows (e.g. MiniMax H3 ref2video) these slots are
    filled in this order and it defines ``<Subject N>`` prompt numbering.
    """
    return _find_all_by_role(inputs, "image")


def ref_video_slots(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``(Input:video)`` slots in node-id order."""
    return _find_all_by_role(inputs, "video")


def ref_audio_slots(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``(Input:audio)`` slots in node-id order."""
    return _find_all_by_role(inputs, "audio")


def _legacy_prompt_node(inputs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Deprecated: untagged (Input) text nodes — prefer (Input:prompt)."""
    for inp in inputs:
        if inp.get("role"):
            continue
        if inp.get("kind") not in ("text", "textarea"):
            continue
        label = (inp.get("label") or "").lower()
        if "negative" in label:
            continue
        if any(k in label for k in ("prompt", "positive", "text")):
            return inp
    for inp in inputs:
        if inp.get("role"):
            continue
        if inp.get("kind") in ("text", "textarea"):
            label = (inp.get("label") or "").lower()
            if "negative" not in label:
                return inp
    return None


def smart_fill_inputs(
    inputs: list[dict[str, Any]],
    *,
    prompt: str | None = None,
    negative_prompt: str | None = None,
    character_image: str | None = None,
    location_image: str | None = None,
    ref_images: list[str] | None = None,
    ref_videos: list[str] | None = None,
    ref_audios: list[str] | None = None,
    duration: int | float | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build valuesByNodeId from workflow dynamic inputs + context.

    Mapping is driven by Comfy title role tags, e.g. ``(Input:prompt)``.
    See ``AGENTS.md`` / README for the role contract.

    Order: non-blank defaults → role-based context → shared extras (non-blank).
    User/form ``extra`` wins so regenerating a clip keeps the settings they edited.
    Blank extras are skipped so an empty shared Text Prompt cannot wipe context.
    """
    values: dict[str, Any] = {}
    for inp in inputs:
        node_id = str(inp["nodeId"])
        default = inp.get("defaultValue")
        if default is None:
            continue
        if isinstance(default, str) and not default.strip():
            continue
        values[node_id] = default

    if prompt:
        target = _find_by_role(inputs, "prompt") or _legacy_prompt_node(inputs)
        if target:
            values[str(target["nodeId"])] = prompt

    if negative_prompt:
        target = _find_by_role(inputs, "negative")
        if target:
            values[str(target["nodeId"])] = negative_prompt
        else:
            for inp in inputs:
                if inp.get("role"):
                    continue
                if inp.get("kind") in ("text", "textarea") and "negative" in (
                    inp.get("label") or ""
                ).lower():
                    values[str(inp["nodeId"])] = negative_prompt
                    break

    if character_image:
        target = _find_by_role(inputs, "character")
        if target:
            values[str(target["nodeId"])] = character_image
        else:
            for inp in inputs:
                if inp.get("role"):
                    continue
                if inp.get("kind") in ("image", "image_url"):
                    label = (inp.get("label") or "").lower()
                    if any(
                        k in label
                        for k in ("character", "portrait", "reference", "sheet", "face", "ref")
                    ) and not any(k in label for k in ("env", "location", "background", "scene")):
                        values[str(inp["nodeId"])] = character_image
                        break

    if location_image:
        target = _find_by_role(inputs, "location")
        if target:
            values[str(target["nodeId"])] = location_image
        else:
            for inp in inputs:
                if inp.get("role"):
                    continue
                if inp.get("kind") in ("image", "image_url"):
                    label = (inp.get("label") or "").lower()
                    if any(
                        k in label
                        for k in ("env", "location", "background", "scene", "setting")
                    ):
                        values[str(inp["nodeId"])] = location_image
                        break

    if ref_images:
        for inp, path in zip(ref_image_slots(inputs), ref_images):
            values[str(inp["nodeId"])] = path

    if ref_videos:
        for inp, path in zip(ref_video_slots(inputs), ref_videos):
            values[str(inp["nodeId"])] = path

    if ref_audios:
        for inp, path in zip(ref_audio_slots(inputs), ref_audios):
            values[str(inp["nodeId"])] = path

    if duration is not None:
        target = _find_by_role(inputs, "duration")
        if target:
            values[str(target["nodeId"])] = duration

    if extra:
        reserved = {
            str(inp["nodeId"])
            for inp in inputs
            if normalize_input_role(inp.get("role")) == "duration"
        }
        for k, v in extra.items():
            if v is None:
                continue
            if isinstance(v, str) and not str(v).strip():
                continue
            if str(k) in reserved:
                continue
            values[str(k)] = v

    return values
