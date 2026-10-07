"""Workspace files plugin: contained scratch-file tools for the agent.

The shell (``system.py``) is opt-in and HITL-gated; these tools are the always-
available core of the agent workspace: the model can draft scripts, beat lists,
shot plans, and video prompts as plain text files while it works, show them to
the user, and commit the approved content into the project with the existing
scene/clip/beat tools. Files persist across turns and are shared between the
main loop and its sub-agents — one scratch folder per session:

    settings.workspace_dir / "sessions" / <session_id>

Containment is stricter than the shell's: every path argument must be RELATIVE,
may not contain ``..`` or a sensitive name (config / db / .env / credentials —
the same deny-list as the shell), and its realpath must stay inside the session
folder (symlink defense, the ``skills_store.read_skill_file`` pattern). Policy
lives in the pre-execute guard; executors are mechanics.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from calliope.agent.harness.registry import (
    GUARD_WORKSPACE_PATH,
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    allow,
    deny,
)
from calliope.agent.harness.plugins.system import _SENSITIVE_NAME_RE
from calliope.config import settings

logger = logging.getLogger("calliope.harness.plugins.files")

READ_CHAR_CAP = 100_000
WRITE_CHAR_CAP = 200_000
LIST_ENTRY_CAP = 200

_FILE_TOOLS = frozenset({"list_files", "read_file", "write_file", "delete_file"})


def session_root(ctx: ToolContext, *, mkdir: bool = False) -> Path:
    """This session's scratch folder under the agent workspace.

    One folder per session (chat and scene origins alike get an id); the main
    loop and its sub-agents share it. Created lazily by the tools, not by a
    prompt render.
    """
    root = settings.workspace_dir / "sessions" / str(ctx.session_id)
    if mkdir:
        root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_session_path(
    ctx: ToolContext, raw: Any, *, required: bool = True
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
        return session_root(ctx, mkdir=True), None
    candidate = Path(text)
    if candidate.is_absolute() or candidate.anchor:
        return None, f"path must be relative to the session folder: {text!r}"
    if ".." in candidate.parts:
        return None, f"path must not contain '..': {text!r}"
    if _SENSITIVE_NAME_RE.search(text):
        return None, f"path names a sensitive file: {text!r}"
    root = session_root(ctx, mkdir=True)
    target = root / candidate
    try:
        resolved = target.resolve()
        resolved.relative_to(root.resolve())
    except (ValueError, OSError):
        return None, f"path escapes the session folder: {text!r}"
    return resolved, None


async def _files_guard(ctx: ToolContext, t: ToolDefinition, args: dict) -> Any:
    """Pre-execute policy: every path arg must stay inside the session folder.

    Only args actually present are checked — optional keys the model omitted
    (list_files without subdir) pass through, and executors re-resolve so a
    missing required path still errors (from the executor, not the guard).
    """
    if t.name not in _FILE_TOOLS:
        return allow()
    for key, required in (("path", True), ("subdir", False)):
        if key not in args:
            continue
        _, err = resolve_session_path(ctx, args.get(key), required=required)
        if err:
            return deny(
                err + " Use relative paths inside the session scratch folder "
                "(list_files with no arguments shows it).",
                code=GUARD_WORKSPACE_PATH,
            )
    return allow()


async def t_list_files(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    base, err = resolve_session_path(ctx, args.get("subdir"), required=False)
    if err:
        return {"ok": False, "error": err}
    if not base.exists():
        return {"ok": True, "root": str(base), "entries": [], "truncated": False}

    def _scan() -> list[dict[str, Any]]:
        children = sorted(
            base.iterdir(), key=lambda p: (p.is_file(), p.name.lower())
        )
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


async def t_read_file(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    path, err = resolve_session_path(ctx, args.get("path"))
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


async def t_write_file(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    path, err = resolve_session_path(ctx, args.get("path"))
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


async def t_delete_file(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    path, err = resolve_session_path(ctx, args.get("path"))
    if err:
        return {"ok": False, "error": err}
    assert path is not None
    if not path.exists():
        return {"ok": False, "error": f"no such file: {args.get('path')}"}
    if path.is_dir():
        return {"ok": False, "error": "refusing to delete a directory — files only"}

    await asyncio.to_thread(path.unlink)
    return {"ok": True, "path": str(path)}


def files_prompt_text(ctx: ToolContext) -> str | None:
    """The scratch-workspace section body (sync — same shape as memory/skills).

    Tells the agent where its session folder is and the draft → show → commit
    workflow. None for Build Scene (origin 'scene') — its toolset is shot-only,
    so advertising file tools there would invite 'tool doesn't exist' loops.
    """
    if ctx.origin == "scene":
        return None
    root = settings.workspace_dir / "sessions" / str(ctx.session_id)
    return (
        "SCRATCH WORKSPACE: you have a persistent folder on disk for drafts:\n"
        f"{root}\n"
        "Use write_file to draft scripts, beat lists, shot plans, and video prompts "
        "as text files while you work; read_file / list_files to revisit them. Files "
        "persist across turns and are shared with your sub-agents — a draft one agent "
        "writes, another can read. Show the draft to the user; when they approve, "
        "commit it into the project with the scene/clip/beat tools (add_scene, "
        "update_scene, update_clip, add_beat, ...). Paths are relative to the session "
        "folder and cannot escape it. The optional shell (run_command) works in the "
        "same workspace when enabled in Settings → Agent."
    )


async def _files_section(ctx: ToolContext) -> str | None:
    return files_prompt_text(ctx)


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="list_files",
            description=(
                "List the files in this session's scratch workspace folder "
                "(workspace/sessions/<session id>). Pass no arguments for the "
                "folder root, or subdir for a nested folder. Scratch files are "
                "text drafts (scripts, beat lists, video prompts) that persist "
                "across turns and are shared with your sub-agents."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "subdir": {
                        "type": "string",
                        "description": "Optional subfolder, relative to the session folder",
                    },
                },
            },
            executor=t_list_files,
            category="system",
            requires_project=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="read_file",
            description=(
                "Read a text file from this session's scratch workspace "
                "(workspace/sessions/<session id>). path is relative to that "
                "folder — list_files shows what exists."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                },
                "required": ["path"],
            },
            executor=t_read_file,
            category="system",
            requires_project=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="write_file",
            description=(
                "Write a text file into this session's scratch workspace — draft "
                "scripts, beat lists, shot plans, and video prompts here, show "
                "them to the user, then commit approved content into the project "
                "with the scene/clip/beat tools. Directories are created as "
                "needed; mode=append adds to an existing file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                    "content": {
                        "type": "string",
                        "description": "UTF-8 text to write",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["overwrite", "append"],
                        "description": "overwrite (default) replaces the file; append adds to it",
                    },
                },
                "required": ["path", "content"],
            },
            executor=t_write_file,
            category="system",
            requires_project=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="delete_file",
            description=(
                "Delete one text file from this session's scratch workspace. "
                "Files only — directories are refused."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                },
                "required": ["path"],
            },
            executor=t_delete_file,
            category="system",
            requires_project=False,
        )
    )
    registry.on_pre_execute(_files_guard)
