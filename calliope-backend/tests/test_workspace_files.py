"""Agent scratch workspace: the contained file tools (plugins/files.py).

The Canvas agent (and every swarm sub-agent) gets list_files / read_file /
write_file / delete_file scoped to ONE folder per session:

    settings.workspace_dir / "sessions" / <session_id>

Everything here locks the containment contract (relative paths only, no `..`,
no sensitive names, realpath stays inside the folder), the per-session
isolation, the visibility contract (sandbox sessions see the tools; Build
Scene does not), and the prompt section that teaches the draft → show →
commit workflow.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from calliope.agent.harness import build_harness
from calliope.agent.harness.orchestrator import ROLE_TOOLS, _scoped_payload
from calliope.agent.harness.plugins.files import (
    files_prompt_text,
    resolve_session_path,
)
from calliope.agent.harness.registry import ToolContext
from calliope.config import settings
from calliope.db import migrate_db


@pytest.fixture()
def workspace_root(tmp_path, monkeypatch):
    """An in-memory workspace override — never saved to the real config.

    Also points ``data_dir`` at this test's temp dir and migrates a fresh DB:
    ``openai_payload`` consults the agent event log (requires_approval
    visibility), and earlier tests' temp DBs are gone by the time this file
    runs in the full suite.
    """
    root = tmp_path / "workspace"
    monkeypatch.setattr(settings, "agent_workspace_dir", root)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    asyncio.run(migrate_db(settings.db_path))
    return root


@pytest.fixture()
def harness(workspace_root):
    registry, prompts = build_harness()
    return registry, prompts


def _ctx(session_id: int = 7) -> ToolContext:
    return ToolContext(session_id=session_id, project_id=None, origin="chat")


# ── roundtrip ────────────────────────────────────────────────────────────────


def test_write_list_read_delete_roundtrip(harness, workspace_root):
    registry, _ = harness
    ctx = _ctx()

    out = asyncio.run(
        registry.execute(ctx, "write_file", {"path": "script-draft.md", "content": "# Scene 1"})
    )
    assert out["ok"] is True
    # Land in THIS session's folder, not the workspace root
    assert out["path"] == str(workspace_root / "sessions" / "7" / "script-draft.md")

    listed = asyncio.run(registry.execute(ctx, "list_files", {}))
    assert listed["ok"] is True
    assert [e["name"] for e in listed["entries"]] == ["script-draft.md"]
    assert listed["entries"][0]["type"] == "file"

    read = asyncio.run(registry.execute(ctx, "read_file", {"path": "script-draft.md"}))
    assert read["ok"] is True
    assert read["content"] == "# Scene 1"
    assert read["truncated"] is False

    deleted = asyncio.run(registry.execute(ctx, "delete_file", {"path": "script-draft.md"}))
    assert deleted["ok"] is True
    assert not (workspace_root / "sessions" / "7" / "script-draft.md").exists()


def test_write_append_and_subdirectories(harness, workspace_root):
    registry, _ = harness
    ctx = _ctx()
    asyncio.run(
        registry.execute(ctx, "write_file", {"path": "prompts/clip-1.txt", "content": "one"})
    )
    asyncio.run(
        registry.execute(
            ctx, "write_file", {"path": "prompts/clip-1.txt", "content": " two", "mode": "append"}
        )
    )
    read = asyncio.run(registry.execute(ctx, "read_file", {"path": "prompts/clip-1.txt"}))
    assert read["content"] == "one two"
    listed = asyncio.run(registry.execute(ctx, "list_files", {"subdir": "prompts"}))
    assert [e["name"] for e in listed["entries"]] == ["clip-1.txt"]


def test_list_files_empty_session_is_ok(harness, workspace_root):
    registry, _ = harness
    out = asyncio.run(registry.execute(_ctx(), "list_files", {}))
    assert out == {
        "ok": True,
        "root": str(workspace_root / "sessions" / "7"),
        "entries": [],
        "truncated": False,
    }


def test_read_missing_file_and_delete_dir_refused(harness, workspace_root):
    registry, _ = harness
    ctx = _ctx()
    missing = asyncio.run(registry.execute(ctx, "read_file", {"path": "nope.txt"}))
    assert missing["ok"] is False and "not a file" in missing["error"]
    d = workspace_root / "sessions" / "7" / "sub"
    d.mkdir(parents=True)
    refused = asyncio.run(registry.execute(ctx, "delete_file", {"path": "sub"}))
    assert refused["ok"] is False and "directory" in refused["error"]


# ── containment ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tool,args",
    [
        ("read_file", {"path": "../other-session/file.md"}),
        ("read_file", {"path": "C:\\Windows\\evil.txt"}),
        ("read_file", {"path": "/etc/passwd"}),
        ("write_file", {"path": ".env", "content": "KEY=1"}),
        ("write_file", {"path": "copy of calliope.db", "content": "x"}),
        ("delete_file", {"path": "creds/id_rsa"}),
        ("list_files", {"subdir": ".."}),
    ],
)
def test_path_containment_denials(harness, workspace_root, tool, args):
    registry, _ = harness
    ctx = _ctx()
    out = asyncio.run(registry.execute(ctx, tool, args))
    assert out["ok"] is False
    assert out["reason_code"] == "guard_workspace_path", out


def test_symlink_escape_is_refused(harness, workspace_root, tmp_path):
    registry, _ = harness
    ctx = _ctx()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace_root / "sessions" / "7"
    link.mkdir(parents=True)
    try:
        (link / "leak.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable on this filesystem")
    out = asyncio.run(registry.execute(ctx, "read_file", {"path": "leak.txt"}))
    assert out["ok"] is False and out["reason_code"] == "guard_workspace_path"


def test_sessions_are_isolated(harness, workspace_root):
    registry, _ = harness
    asyncio.run(
        registry.execute(_ctx(7), "write_file", {"path": "a.md", "content": "mine"})
    )
    other = _ctx(8)
    listed = asyncio.run(registry.execute(other, "list_files", {}))
    assert listed["entries"] == []
    crossed = asyncio.run(registry.execute(other, "read_file", {"path": "../7/a.md"}))
    assert crossed["ok"] is False


def test_resolve_requires_relative_paths(workspace_root):
    ctx = _ctx()
    path, err = resolve_session_path(ctx, "notes/draft.md")
    assert err is None and path == workspace_root / "sessions" / "7" / "notes" / "draft.md"
    _, err = resolve_session_path(ctx, "", required=True)
    assert err == "path is required"


# ── visibility + roles ───────────────────────────────────────────────────────


def test_file_tools_visible_in_sandbox(harness):
    """requires_project=False is load-bearing: blind sessions must see them."""
    registry, _ = harness
    payload = registry.openai_payload(_ctx())
    names = {t["function"]["name"] for t in payload}
    assert {"list_files", "read_file", "write_file", "delete_file"} <= names


def test_file_tools_hidden_in_build_scene(harness):
    """origin='scene' is the shot-only surface — no file tools, no section."""
    registry, prompts = harness
    ctx = ToolContext(session_id=9, project_id=None, origin="scene")
    names = {t["function"]["name"] for t in registry.openai_payload(ctx)}
    assert not names & {"list_files", "read_file", "write_file", "delete_file"}
    section = asyncio.run(prompts.assemble(ctx))
    assert "SCRATCH WORKSPACE" not in section


def test_every_role_gets_the_file_tools():
    for role, tools in ROLE_TOOLS.items():
        assert {"list_files", "read_file", "write_file", "delete_file"} <= set(tools), role


def test_sub_agent_scoped_payload_keeps_file_tools():
    ctx = _ctx()
    for role in ROLE_TOOLS:
        names = {t["function"]["name"] for t in _scoped_payload(ctx, ROLE_TOOLS[role])}
        assert {"write_file", "read_file"} <= names, role


# ── prompt section ───────────────────────────────────────────────────────────


def test_prompt_section_renders_session_folder(harness, workspace_root):
    _, prompts = harness
    section = asyncio.run(prompts.assemble(_ctx()))
    assert "SCRATCH WORKSPACE" in section
    assert str(workspace_root / "sessions" / "7") in section
    assert "update_scene" in section  # teaches the commit path
    # The sync helper mirrors the section (used by _run_sub_agent).
    assert files_prompt_text(_ctx()) is not None


def test_file_tools_in_contracts():
    """The four tool names + the guard code are contract vocabulary."""
    contracts = json.loads(
        (Path(__file__).resolve().parents[1] / "contracts.json").read_text(encoding="utf-8")
    )
    tools = set(contracts.get("agent_tool_names") or [])
    for name in ("list_files", "read_file", "write_file", "delete_file"):
        assert name in tools, name
    codes = set(contracts.get("guard_codes") or [])
    assert "guard_workspace_path" in codes
