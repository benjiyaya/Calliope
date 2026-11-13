"""Scratch-file tools for the agent workspace.

The shell (``shell.py``) is opt-in and HITL-gated; these tools are the always-
available core of the agent workspace: the model can draft scripts, beat lists,
shot plans, and video prompts as plain text files while it works, show them to
the user, and commit the approved content into the project with the existing
scene/clip/beat tools. Files persist across turns and are shared between the
grants of one session — one scratch folder per session:

    settings.workspace_dir / "sessions" / <session_id>

Containment is stricter than the shell's: every path argument must be RELATIVE,
may not contain ``..`` or a sensitive name (config / db / .env / credentials —
the same deny-list as the shell), and its realpath must stay inside the session
folder (symlink defense, the ``skills_store.read_skill_file`` pattern).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from calliope.agent.capabilities.shell import _SENSITIVE_NAME_RE
from calliope.config import settings

logger = logging.getLogger("calliope.agent.capabilities.workspace")

READ_CHAR_CAP = 100_000
WRITE_CHAR_CAP = 200_000
LIST_ENTRY_CAP = 200


def session_root(session_id: int | None, *, mkdir: bool = False) -> Path:
    """This session's scratch folder under the agent workspace.

    One folder per session (chat and scene origins alike get an id); every
    grant of the session shares it. Created lazily by the tools, not by a
    prompt render.
    """
    root = settings.workspace_dir / "sessions" / str(session_id or 0)
    if mkdir:
        root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_session_path(
    session_id: int | None, raw: Any, *, required: bool = True
) -> tuple[Path | None, str | None]:
    """Resolve a tool path argument inside the session scratch folder.

    Returns ``(path, error)``. An empty value resolves to the folder root when
    ``required=False`` (list_files' ``subdir``). Rejects absolute paths, ``..``
    segments, sensitive names, and anything whose realpath escapes the session
    root.
    """
    text = str(raw or "").strip().strip('"').strip()
    if not text:
        if required:
            return None, "path is required"
        return session_root(session_id, mkdir=True), None
    candidate = Path(text)
    if candidate.is_absolute() or candidate.anchor:
        return None, f"path must be relative to the session folder: {text!r}"
    if ".." in candidate.parts:
        return None, f"path must not contain '..': {text!r}"
    if _SENSITIVE_NAME_RE.search(text):
        return None, f"path names a sensitive file: {text!r}"
    root = session_root(session_id, mkdir=True)
    target = root / candidate
    try:
        resolved = target.resolve()
        resolved.relative_to(root.resolve())
    except (ValueError, OSError):
        return None, f"path escapes the session folder: {text!r}"
    return resolved, None


# ─────────────────────────────────────────────────────────────────────────
# Executors
# ─────────────────────────────────────────────────────────────────────────


async def list_files(session_id: int | None, args: dict[str, Any]) -> dict[str, Any]:
    base, err = resolve_session_path(session_id, args.get("subdir"), required=False)
    if err:
        return {"ok": False, "error": err}
    assert base is not None
    if not base.exists():
        return {"ok": True, "root": str(base), "entries": [], "truncated": False}

    def _scan() -> list[dict[str, Any]]:
        children = sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        out: list[dict[str, Any]] = []
        for child in children:
            if len(out) >= LIST_ENTRY_CAP:
                break
            try:
                stat = child.stat()
            except OSError:
                continue
            out.append(
                {
                    "name": child.name,
                    "type": "dir" if child.is_dir() else "file",
                    "size": stat.st_size if child.is_file() else None,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(
                        timespec="seconds"
                    ),
                }
            )
        return out

    entries = await asyncio.to_thread(_scan)
    return {
        "ok": True,
        "root": str(base),
        "entries": entries,
        "truncated": len(entries) >= LIST_ENTRY_CAP,
    }


async def read_file(session_id: int | None, args: dict[str, Any]) -> dict[str, Any]:
    path, err = resolve_session_path(session_id, args.get("path"))
    if err:
        return {"ok": False, "error": err}
    assert path is not None
    if not path.is_file():
        return {"ok": False, "error": f"not a file: {args.get('path')}"}

    def _read() -> str:
        return path.read_text(encoding="utf-8", errors="replace")

    text = await asyncio.to_thread(_read)
    truncated = len(text) > READ_CHAR_CAP
    if truncated:
        text = text[:READ_CHAR_CAP]
    return {
        "ok": True,
        "path": str(path),
        "content": text,
        "chars": len(text),
        "truncated": truncated,
    }


async def write_file(session_id: int | None, args: dict[str, Any]) -> dict[str, Any]:
    path, err = resolve_session_path(session_id, args.get("path"))
    if err:
        return {"ok": False, "error": err}
    assert path is not None
    content = args.get("content")
    text = "" if content is None else str(content)
    if len(text) > WRITE_CHAR_CAP:
        return {
            "ok": False,
            "error": f"content too large ({len(text)} chars; cap is {WRITE_CHAR_CAP}) "
            "— write it in parts with mode=append",
        }
    mode = str(args.get("mode") or "overwrite")
    if mode not in ("overwrite", "append"):
        return {"ok": False, "error": f"mode must be 'overwrite' or 'append', got {mode!r}"}

    def _write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a" if mode == "append" else "w", encoding="utf-8", newline="") as fh:
            fh.write(text)

    await asyncio.to_thread(_write)
    return {"ok": True, "path": str(path), "mode": mode, "chars": len(text)}


async def delete_file(session_id: int | None, args: dict[str, Any]) -> dict[str, Any]:
    path, err = resolve_session_path(session_id, args.get("path"))
    if err:
        return {"ok": False, "error": err}
    assert path is not None
    if not path.exists():
        return {"ok": False, "error": f"no such file: {args.get('path')}"}
    if path.is_dir():
        return {"ok": False, "error": "refusing to delete a directory — files only"}

    await asyncio.to_thread(path.unlink)
    return {"ok": True, "path": str(path)}


def files_prompt_text(session_id: int | None, origin: str = "chat") -> str | None:
    """The scratch-workspace prompt section body.

    Tells the agent where its session folder is and the draft → show → commit
    workflow. None for Build Scene (origin 'scene') — its grant is shot-only,
    so advertising file tools there would invite 'tool doesn't exist' loops.
    """
    if origin == "scene":
        return None
    root = session_root(session_id)
    return (
        "SCRATCH WORKSPACE: you have a persistent folder on disk for drafts:\n"
        f"{root}\n"
        "Use write_file to draft scripts, beat lists, shot plans, and video prompts "
        "as text files while you work; read_file / list_files to revisit them. Files "
        "persist across turns — a draft written this turn can be read next turn. "
        "Show the draft to the user; when they approve, commit it into the project "
        "with the scene/clip/beat tools (add_scene, update_scene, update_clip, "
        "add_beat, ...). Paths are relative to the session folder and cannot "
        "escape it. The optional shell (run_command) works in the same workspace "
        "when enabled in Settings → Agent."
    )
