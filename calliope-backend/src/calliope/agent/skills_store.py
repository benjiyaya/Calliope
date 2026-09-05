"""Skills storage: the single home for SKILLS-directory primitives.

Used by the agent plugin (harness/plugins/skills.py), the Settings API, and
the composer's /-picker endpoint. Lives OUTSIDE harness/plugins so non-harness
callers (Settings router) don't import tool plumbing (ToolContext etc.).

Layout: `<data_dir>/skills/<skill-name>/SKILL.md` (YAML frontmatter) plus any
files the body references (e.g. references/*.md). Builtins ship in
`calliope-backend/skills_builtin/` and are seeded once — never overwriting
user edits.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

from calliope.config import settings

READ_CHAR_CAP = 8000
_LIST_CAP = 24
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def skills_root() -> Path:
    """The user-editable skills directory: `<data_dir>/skills`."""
    return Path(settings.data_dir) / "skills"


def builtin_root() -> Path:
    """Vendored read-only builtins shipped with the backend."""
    return Path(__file__).resolve().parents[3] / "skills_builtin"


def ensure_builtin_skills() -> None:
    """Seed builtin skills once per data_dir. Never overwrites: an existing
    skill dir (user-editable) always wins over the vendored copy."""
    src_root = builtin_root()
    if not src_root.is_dir():
        return
    dst_root = skills_root()
    dst_root.mkdir(parents=True, exist_ok=True)
    for skill_dir in sorted(src_root.iterdir()):
        if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
            continue
        target = dst_root / skill_dir.name
        if target.exists():
            continue
        try:
            shutil.copytree(skill_dir, target)
        except OSError:
            pass


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Minimal YAML frontmatter reader (flat keys, inline lists, the
    metadata block)."""

    def _coerce(value: str) -> Any:
        """Inline `[a, b]` lists become Python lists — the API contract is
        `tags: string[]`, and a bare string broke the Settings list render."""
        v = value.strip()
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            if not inner:
                return []
            return [item.strip().strip("'\"") for item in inner.split(",") if item.strip()]
        return v.strip("'\"")

    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    meta: dict[str, Any] = {}
    current_dict: str | None = None
    for raw in match.group(1).splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if ":" not in line:
            # Multi-line `- item` lists are not used by any builtin or the
            # documented format (inline [a, b] is); ignore strays silently.
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if indent == 0:
            current_dict = None
            meta[key] = _coerce(value) if value.strip() else {}
            if not value.strip():
                current_dict = key
        elif current_dict is not None:
            block = meta[current_dict]
            if isinstance(block, dict):
                block[key] = _coerce(value)
        elif isinstance(meta.get("metadata"), dict):
            meta["metadata"][key] = _coerce(value)
    return meta


def skill_summary(skill_dir: Path) -> dict[str, Any] | None:
    """Frontmatter view of one skill dir (None when SKILL.md is absent/bad)."""
    skill_file = skill_dir / "SKILL.md"
    try:
        text = skill_file.read_text(encoding="utf-8")
    except OSError:
        return None
    meta = _parse_frontmatter(text)
    if not meta:
        return None
    metadata = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    return {
        "name": str(meta.get("name") or skill_dir.name),
        "description": str(meta.get("description") or "").strip(),
        "version": str(meta.get("version") or ""),
        "tags": metadata.get("tags") or [],
        "dir": skill_dir.name,
    }


def list_skills() -> list[dict[str, Any]]:
    """Frontmatter summaries for every `*/SKILL.md`, sorted by name."""
    ensure_builtin_skills()
    root = skills_root()
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for skill_dir in sorted(root.iterdir()):
        if not skill_dir.is_dir():
            continue
        summary = skill_summary(skill_dir)
        if summary:
            out.append(summary)
        if len(out) >= _LIST_CAP:
            break
    return out


def skills_prompt_lines() -> list[str]:
    """`- name: description` lines for the agent's discovery section."""
    return [f"- {s['name']}: {s['description']}" for s in list_skills()]


def skill_files(name: str) -> list[str] | None:
    """Relative file listing for a skill dir (None when unknown)."""
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return None
    candidate = skills_root() / name
    if not candidate.is_dir():
        return None
    return sorted(
        str(p.relative_to(candidate)).replace("\\", "/")
        for p in candidate.rglob("*")
        if p.is_file()
    )


def read_skill_file(name: str, rel_path: str = "SKILL.md") -> dict[str, Any]:
    """Read one file from a skill dir, realpath-containment-checked (no `..`,
    no symlink escape — fail closed like training_routes)."""
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return {"ok": False, "error": f"unknown skill '{name}'"}
    skill_dir = skills_root() / name
    if not skill_dir.is_dir():
        return {
            "ok": False,
            "error": f"unknown skill '{name}'",
            "available": [s["name"] for s in list_skills()],
        }
    rel = (rel_path or "SKILL.md").strip().replace("\\", "/")
    candidate = skill_dir / rel
    root_real = os.path.realpath(skill_dir)
    file_real = os.path.realpath(candidate)
    try:
        contained = Path(file_real).is_relative_to(Path(root_real))
    except (OSError, ValueError):
        contained = False
    if not contained or not Path(file_real).is_file():
        return {
            "ok": False,
            "error": f"path not found in skill '{name}': {rel}",
            "files": skill_files(name) or [],
        }
    try:
        text = Path(file_real).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"ok": False, "error": f"unreadable file: {rel}"}
    truncated = len(text) > READ_CHAR_CAP
    return {
        "ok": True,
        "skill": name,
        "path": rel,
        "content": text[:READ_CHAR_CAP] if truncated else text,
        "truncated": truncated,
    }
