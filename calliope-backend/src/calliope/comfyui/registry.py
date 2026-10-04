"""Unified class_type → kind + patch-field registry (MuseAgent port)."""
from __future__ import annotations

from typing import Literal

ComfyInputKind = Literal["text", "textarea", "number", "image", "image_url", "audio", "video"]
ComfyOutputKind = Literal["image", "video", "other"]
PatchField = Literal[
    "image", "url", "audio", "video", "file", "text", "value", "int", "float", "prompt"
]

TEXT_AREA_CLASSES = frozenset(
    {
        "CLIPTextEncode",
        "Note",
        "ShowText",
        "ImpactWildcardProcessor",
        "PrimitiveStringMultiline",
        "PrimitiveString",
    }
)
NUMBER_CLASSES = frozenset(
    {"INT", "FLOAT", "PrimitiveInt", "PrimitiveFloat", "KSampler", "KSamplerAdvanced"}
)
IMAGE_CLASSES = frozenset({"LoadImage", "ImageLoader", "ETN_LoadImageBase64"})
IMAGE_URL_CLASSES = frozenset({"Load Image From Url (mtb)"})
AUDIO_CLASSES = frozenset({"LoadAudio", "VHS_LoadAudio"})
# Stock LoadVideo uses the `file` widget; VHS uses `video`.
VIDEO_CLASSES = frozenset({"LoadVideo", "VHS_LoadVideo", "VHS_LoadVideoPath"})
VIDEO_FILE_CLASSES = frozenset({"LoadVideo"})
VIDEO_OUTPUT_CLASSES = frozenset(
    {"VHS_VideoCombine", "SaveVideo", "VideoOutput", "AnimateDiffCombine"}
)
IMAGE_OUTPUT_CLASSES = frozenset(
    {
        "SaveImage",
        "SaveImageAdvanced",
        "PreviewImage",
        "SaveImageWebsocket",
        "ETN_SendImageWebSocket",
    }
)
# Generator / encoder nodes whose user-facing text widget is named `prompt`.
# They need listing explicitly: the substring fallback below matches "image" or
# "video" in the class name first, which made `TextEncodeQwenImage21` resolve to
# an image input and patch a prompt straight into a real image link.
PROMPT_CLASSES = frozenset(
    {
        "MiniMaxH3ImageToVideo",
        "MiniMaxH3ReferenceToVideo",
        "TextEncodeQwenImage21",
        "TextEncodeQwenImage",
        "TextGenerate",
    }
)
# Loader / utility nodes with no media widget Calliope should ever target.
# Listed so the "load" / "image" / "video" substrings stop calling them media.
NEUTRAL_CLASSES = frozenset(
    {
        "CCTechClipProjLoader",
        "UnetLoaderGGUF",
        "QwenImage21Cache",
        "ForceUnloadBeforeDecode",
        "GetVideoComponents",
    }
)


def class_to_input_kind(class_type: str) -> ComfyInputKind:
    if class_type in IMAGE_CLASSES:
        return "image"
    if class_type in IMAGE_URL_CLASSES:
        return "image_url"
    if class_type in AUDIO_CLASSES:
        return "audio"
    if class_type in VIDEO_CLASSES:
        return "video"
    if class_type in NUMBER_CLASSES:
        return "number"
    if class_type in TEXT_AREA_CLASSES:
        return "textarea"
    if class_type in PROMPT_CLASSES:
        return "textarea"
    if class_type in NEUTRAL_CLASSES:
        return "text"
    lower = class_type.lower()
    if "video" in lower:
        return "video"
    if "audio" in lower:
        return "audio"
    if "image" in lower or "load" in lower:
        return "image"
    if "int" in lower or "float" in lower or "seed" in lower:
        return "number"
    if "text" in lower or "clip" in lower or "prompt" in lower:
        return "textarea"
    return "text"


def class_to_patch_field(class_type: str) -> PatchField:
    if class_type in IMAGE_CLASSES:
        return "image"
    if class_type in IMAGE_URL_CLASSES:
        return "url"
    if class_type in AUDIO_CLASSES:
        return "audio"
    if class_type in VIDEO_FILE_CLASSES:
        return "file"
    if class_type in VIDEO_CLASSES:
        return "video"
    if class_type in {"CLIPTextEncode", "Note", "ShowText", "ImpactWildcardProcessor"}:
        return "text"
    if class_type.startswith("Primitive"):
        return "value"
    if class_type in PROMPT_CLASSES:
        return "prompt"
    kind = class_to_input_kind(class_type)
    if kind == "image":
        return "image"
    if kind == "image_url":
        return "url"
    if kind == "audio":
        return "audio"
    if kind == "video":
        return "file" if class_type in VIDEO_FILE_CLASSES else "video"
    if kind == "textarea":
        return "text"
    return "value"


def class_to_output_kind(class_type: str) -> ComfyOutputKind:
    if class_type in VIDEO_OUTPUT_CLASSES or "video" in class_type.lower():
        return "video"
    if class_type in IMAGE_OUTPUT_CLASSES or "image" in class_type.lower():
        return "image"
    return "other"
