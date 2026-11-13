"""Read skill folders for the composer. No copy into the data dir."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from calliope.config import settings

_BUILTIN = Path(__file__).resolve().parents[3] / "skills_builtin"


def skills_root() -> Path:
    return settings.data_dir / "skills"


def _dirs() -> list[Path]:
    found: list[Path] = []
    user = skills_root()
    if user.is_dir():
        found.append(user)
    if _BUILTIN.is_dir():
        found.append(_BUILTIN)
    return found


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    meta: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if not line or line[0] in " \t" or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta


def _skill_dirs() -> dict[str, Path]:
    """User folders win when a builtin has the same name."""
    found: dict[str, Path] = {}
    for root in reversed(_dirs()):
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and (entry / "SKILL.md").is_file():
                found[entry.name] = entry
    return found


def list_skills() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for name, folder in _skill_dirs().items():
        meta = _frontmatter((folder / "SKILL.md").read_text(encoding="utf-8"))
        rows.append({"name": name, "description": meta.get("description") or ""})
    return rows


def skill_files(name: str) -> list[str] | None:
    folder = _skill_dirs().get(name)
    if folder is None:
        return None
    files: list[str] = []
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            files.append(path.relative_to(folder).as_posix())
    return files


def read_skill_file(name: str, path: str = "SKILL.md") -> dict[str, Any]:
    folder = _skill_dirs().get(name)
    if folder is None:
        return {"ok": False, "error": f"Unknown skill: {name}"}
    target = (folder / path).resolve()
    try:
        target.relative_to(folder.resolve())
    except ValueError:
        return {"ok": False, "error": "path escapes the skill folder"}
    if not target.is_file():
        return {"ok": False, "error": "file not found"}
    return {"ok": True, "path": path, "content": target.read_text(encoding="utf-8")}
