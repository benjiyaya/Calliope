"""Adversarial security tests for the capability modules.

The old harness guards (registry pre-execute hooks, per-branch guard codes)
are gone. What survives as security contracts in the current architecture:

- shell (``capabilities/shell.py``): sensitive names — calliope_config.json /
  calliope.db / .env / credentials / id_rsa / *.pem/.pfx/.p12 — are denied
  BEFORE any process spawns, even on a read-only command; a disabled shell
  denies everything first;
- workspace (``capabilities/workspace.py``): path escapes ('..', absolute
  paths, symlink realpath escapes) are refused at path resolution;
- ``skills_store.read_skill_file``: traversal out of a skill folder is
  refused on the resolved path (same realpath pattern as the workspace);
- ``catalog.execute``: project-scoped tools fail CLOSED in an unlinked
  (sandbox) chat with an actionable 'not linked' error, while the system
  scratch tools (list_files / read_file / write_file / delete_file) and the
  shell keep working unlinked.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from calliope.agent.capabilities import catalog, shell, workspace
from calliope.agent.skills_store import read_skill_file
from calliope.config import settings

requires_where = pytest.mark.skipif(
    shutil.which("where") is None, reason="where is not on PATH"
)


@pytest.fixture()
def board(tmp_path, monkeypatch):
    """Temp data_dir/workspace/assets. No DB: the paths exercised here deny
    before any event-log or project-table access."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "assets_dir", tmp_path / "assets")
    monkeypatch.setattr(settings, "agent_workspace_dir", None)
    monkeypatch.setattr(settings, "agent_shell_enabled", False)
    return tmp_path


# ─────────────────────────────────────────────────────────────────────────
# Shell: sensitive-name denial
# ─────────────────────────────────────────────────────────────────────────


@requires_where
def test_shell_denies_sensitive_names_even_on_read_only(board, monkeypatch):
    """The config carries API keys and the DB carries every session — no
    command, however read-only, may receive one as an argument."""
    monkeypatch.setattr(settings, "agent_shell_enabled", True)
    for arg in (
        "calliope_config.json",
        "calliope.db",
        ".env",
        "creds/id_rsa",
        "apikey.pem",
        "backup.p12",
        "My-SECRET.txt",
    ):
        out = asyncio.run(shell.run_command(None, {"command": "where", "args": [arg]}))
        assert out["ok"] is False, arg
        assert "sensitive file" in out["error"], arg


def test_shell_disabled_denies_everything(board):
    """Guard order: with the shell off, the enable guidance fires first —
    nothing runs, hostile args included."""
    out = asyncio.run(
        shell.run_command(None, {"command": "where", "args": ["calliope_config.json"]})
    )
    assert out["ok"] is False
    assert "Settings" in out["error"]


# ─────────────────────────────────────────────────────────────────────────
# Workspace: path escape denial
# ─────────────────────────────────────────────────────────────────────────


def test_workspace_rejects_traversal_and_absolute_paths(board):
    out = asyncio.run(workspace.read_file(7, {"path": "../../calliope_config.json"}))
    assert out["ok"] is False and "'..'" in out["error"]

    out = asyncio.run(
        workspace.write_file(7, {"path": "C:\\Users\\evil\\x.txt", "content": "x"})
    )
    assert out["ok"] is False and "relative" in out["error"]

    out = asyncio.run(workspace.delete_file(7, {"path": "/etc/passwd"}))
    assert out["ok"] is False and "relative" in out["error"]

    out = asyncio.run(workspace.list_files(7, {"subdir": ".."}))
    assert out["ok"] is False


def test_session_workspace_realpath_containment(board, tmp_path):
    """A symlink planted inside the session folder must not become a read
    window: resolve_session_path checks the REAL path against the session
    root, so the read dies and the target file survives."""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    root = workspace.session_root(7, mkdir=True)
    try:
        (root / "leak.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable on this filesystem (win32 without privilege)")

    out = asyncio.run(workspace.read_file(7, {"path": "leak.txt"}))
    assert out["ok"] is False and "escapes the session folder" in out["error"]

    # the resolver agrees with the tool guard
    path, err = workspace.resolve_session_path(7, "leak.txt")
    assert path is None and err is not None and "escapes" in err
    assert outside.exists()


# ─────────────────────────────────────────────────────────────────────────
# skills_store: skill-folder traversal guard
# ─────────────────────────────────────────────────────────────────────────


def test_skills_store_read_within_skill(board):
    skill_dir = settings.data_dir / "skills" / "probe-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: probe-skill\ndescription: probe\n---\n# Probe body\n",
        encoding="utf-8",
    )
    ok = read_skill_file("probe-skill")
    assert ok["ok"] is True
    assert "# Probe body" in ok["content"]


def test_skills_store_rejects_traversal(board):
    """../ escapes (and absolute paths) resolve OUTSIDE the skill folder —
    refused before any file read, exactly like the session workspace."""
    skill_dir = settings.data_dir / "skills" / "probe-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("body\n", encoding="utf-8")

    for bad in (
        "../../calliope_config.json",
        "..\\..\\calliope.db",
        str(Path.home() / "secrets.txt"),
    ):
        out = read_skill_file("probe-skill", bad)
        assert out["ok"] is False, bad
        assert "escapes the skill folder" in out["error"], bad

    missing = read_skill_file("probe-skill", "nope.md")
    assert missing == {"ok": False, "error": "file not found"}

    unknown = read_skill_file("no-such-skill")
    assert unknown["ok"] is False and "Unknown skill" in unknown["error"]


# ─────────────────────────────────────────────────────────────────────────
# catalog: unlinked-chat fail-closed
# ─────────────────────────────────────────────────────────────────────────


def test_catalog_project_tools_fail_closed_when_unlinked(board):
    """An unlinked (sandbox) chat must not reach project tables: every
    project-scoped tool returns the actionable link error."""
    for name, args in (
        ("add_scene", {"heading": "X"}),
        ("list_scenes", {}),
        ("delete_beat", {"beat_id": 1}),
    ):
        out = asyncio.run(catalog.execute(name, args, None, 7))
        assert out["ok"] is False, name
        assert "not linked to a project" in out["error"], name


def test_catalog_system_tools_work_unlinked(board):
    """The scratch-file tools (and the shell) are the unlinked half of the
    workspace story — they must not inherit the project gate."""
    out = asyncio.run(catalog.execute("list_files", {}, None, 7))
    assert out["ok"] is True, out

    out = asyncio.run(
        catalog.execute("write_file", {"path": "draft.md", "content": "hello"}, None, 7)
    )
    assert out["ok"] is True
    assert (workspace.session_root(7) / "draft.md").is_file()

    # run_command routes through catalog too; disabled here → enable guidance
    out = asyncio.run(catalog.execute("run_command", {"command": "whoami"}, None, 7))
    assert out["ok"] is False
    assert "Settings" in out["error"]
