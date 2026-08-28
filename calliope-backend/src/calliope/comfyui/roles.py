"""ComfyUI node title role tags: (Input:prompt), (Output:image), etc."""
from __future__ import annotations

import re
from typing import Any

# Title tag: (Input), (Input:prompt), (Output:image), …
_TITLE_TAG_RE = re.compile(
    r"\((?P<kind>Input|Output)(?::(?P<role>[a-zA-Z0-9_-]+))?\)",
    re.IGNORECASE,
)

# Canonical role → accepted aliases (all lowercased).
INPUT_ROLE_ALIASES: dict[str, frozenset[str]] = {
    "prompt": frozenset({"prompt", "positive"}),
    "negative": frozenset({"negative", "neg"}),
    "width": frozenset({"width", "w"}),
    "height": frozenset({"height", "h"}),
    "character": frozenset({"character", "char", "portrait", "sheet", "face", "ref"}),
    "location": frozenset({"location", "loc", "environment", "env", "background", "scene"}),
    "image": frozenset({"image", "img"}),
    "video": frozenset({"video", "vid"}),
    "audio": frozenset({"audio", "sound", "sfx"}),
    "seed": frozenset({"seed"}),
    "duration": frozenset({"duration", "dur", "length", "seconds"}),
}

OUTPUT_ROLE_ALIASES: dict[str, frozenset[str]] = {
    "image": frozenset({"image", "img"}),
    "video": frozenset({"video", "vid"}),
}


def parse_title_tag(title: str) -> tuple[str | None, str | None, str]:
    """Return (kind, role, label) from a Comfy node title.

    kind is ``\"input\"`` / ``\"output\"`` or None if no tag.
    role is the lowercase role string or None for plain ``(Input)`` / ``(Output)``.
    label is the display name with the tag stripped.
    """
    if not title:
        return None, None, ""
    match = _TITLE_TAG_RE.search(title)
    if not match:
        return None, None, title.strip()
    kind = match.group("kind").lower()
    role_raw = match.group("role")
    role = role_raw.lower() if role_raw else None
    stripped = _TITLE_TAG_RE.sub("", title).strip()
    # Title is only the tag, e.g. "(Input:prompt)" → use role as display label.
    if stripped:
        label = stripped
    elif role:
        label = role
    else:
        label = kind
    return kind, role, label


def normalize_input_role(role: str | None) -> str | None:
    """Map an alias to its canonical input role, or return the role as-is if unknown."""
    if not role:
        return None
    r = role.lower().strip()
    for canonical, aliases in INPUT_ROLE_ALIASES.items():
        if r in aliases or r == canonical:
            return canonical
    return r


def normalize_output_role(role: str | None) -> str | None:
    if not role:
        return None
    r = role.lower().strip()
    for canonical, aliases in OUTPUT_ROLE_ALIASES.items():
        if r in aliases or r == canonical:
            return canonical
    return r


def input_has_role(inp: dict[str, Any], *canonical_roles: str) -> bool:
    """True if this parsed input's role matches any of the canonical roles (incl. aliases)."""
    role = normalize_input_role(inp.get("role"))
    if not role:
        return False
    wanted = {normalize_input_role(c) for c in canonical_roles}
    return role in wanted
