"""Agent scratch workspace: the contained file tools (capabilities/workspace.py).

The agent's always-on scratch tools — ``list_files`` / ``read_file`` /
``write_file`` / ``delete_file`` (catalog routes them by name) — are scoped
to ONE folder per session:

    settings.workspace_dir / "sessions" / <session_id>

Every test here locks the containment contract (relative paths only, no ``..``,
no sensitive names — the same deny-list as the shell, and the realpath must
stay inside the folder), per-session isolation, the size caps (read 100k
chars / write 200k chars / list 200 entries), directory-delete refusal, and
the prompt section that teaches the draft → show → commit workflow (``None``
on the Build Scene surface, whose grant is shot-only).

These call the capability functions directly; the catalog indirection for an
unlinked chat is covered in test_agent_security.py.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from calliope.agent.capabilities import workspace
from calliope.config import settings

SID = 7


@pytest.fixture()
def ws(tmp_path, monkeypatch) -> Path:
    """Temp data_dir → the default workspace root (data_dir/workspace).

    ``agent_workspace_dir`` stays None so the Settings → System Paths override
    default is exercised; no DB is needed — the file tools never touch it."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "assets_dir", tmp_path / "assets")
    monkeypatch.setattr(settings, "agent_workspace_dir", None)
    return settings.workspace_dir


def _root(tmp: Path) -> Path:
    return tmp / "sessions" / str(SID)


# ─────────────────────────────────────────────────────────────────────────
# Session folder layout
# ─────────────────────────────────────────────────────────────────────────


def test_session_root_layout(ws):
    root = workspace.session_root(SID)
    assert root == ws / "sessions" / "7"
    assert not root.exists(), "folders are created lazily by the tools"
    workspace.session_root(SID, mkdir=True)
    assert root.is_dir()


def test_session_root_without_session_uses_zero_bucket(ws):
    """A None session id still gets a folder — sandbox scratch must not crash."""
    assert workspace.session_root(None) == ws / "sessions" / "0"


# ─────────────────────────────────────────────────────────────────────────
# Roundtrip: write → list → read → append → delete
# ─────────────────────────────────────────────────────────────────────────


def test_write_list_read_delete_roundtrip(ws):
    root = _root(ws)
    out = asyncio.run(
        workspace.write_file(SID, {"path": "script-draft.md", "content": "# Scene 1"})
    )
    assert out["ok"] is True
    # Lands in THIS session's folder, not the workspace root
    assert Path(out["path"]) == (root / "script-draft.md").resolve()

    listed = asyncio.run(workspace.list_files(SID, {}))
    assert listed["ok"] is True
    assert [e["name"] for e in listed["entries"]] == ["script-draft.md"]
    assert listed["entries"][0]["type"] == "file"
    assert listed["entries"][0]["size"] == 9

    read = asyncio.run(workspace.read_file(SID, {"path": "script-draft.md"}))
    assert read["ok"] is True
    assert read["content"] == "# Scene 1"
    assert read["chars"] == 9
    assert read["truncated"] is False

    deleted = asyncio.run(workspace.delete_file(SID, {"path": "script-draft.md"}))
    assert deleted["ok"] is True
    assert not (root / "script-draft.md").exists()
    again = asyncio.run(workspace.read_file(SID, {"path": "script-draft.md"}))
    assert again["ok"] is False and "not a file" in again["error"]


def test_append_and_subdirectory_listing(ws):
    asyncio.run(workspace.write_file(SID, {"path": "prompts/clip-1.txt", "content": "one"}))
    asyncio.run(
        workspace.write_file(
            SID, {"path": "prompts/clip-1.txt", "content": " two", "mode": "append"}
        )
    )
    read = asyncio.run(workspace.read_file(SID, {"path": "prompts/clip-1.txt"}))
    assert read["content"] == "one two"

    listed = asyncio.run(workspace.list_files(SID, {"subdir": "prompts"}))
    assert [e["name"] for e in listed["entries"]] == ["clip-1.txt"]
    top = asyncio.run(workspace.list_files(SID, {}))
    assert [e["name"] for e in top["entries"]] == ["prompts"]
    assert top["entries"][0]["type"] == "dir"


def test_write_rejects_unknown_mode(ws):
    out = asyncio.run(
        workspace.write_file(SID, {"path": "x.txt", "content": "a", "mode": "truncate"})
    )
    assert out["ok"] is False and "mode" in out["error"]


def test_list_files_empty_session(ws):
    out = asyncio.run(workspace.list_files(SID, {}))
    # Listing CREATES the session folder (resolve with required=False mkdirs).
    assert out == {
        "ok": True,
        "root": str(_root(ws).resolve()),
        "entries": [],
        "truncated": False,
    }


def test_read_missing_file(ws):
    out = asyncio.run(workspace.read_file(SID, {"path": "nope.txt"}))
    assert out["ok"] is False and "not a file" in out["error"]


# ─────────────────────────────────────────────────────────────────────────
# Size caps
# ─────────────────────────────────────────────────────────────────────────


def test_write_cap(ws):
    big = "x" * (workspace.WRITE_CHAR_CAP + 1)
    out = asyncio.run(workspace.write_file(SID, {"path": "big.txt", "content": big}))
    assert out["ok"] is False
    assert "too large" in out["error"]
    assert not (_root(ws) / "big.txt").exists()


def test_read_truncates_over_the_cap(ws):
    """A file over the read cap comes back truncated with an explicit marker
    — not an error, so the agent can still see the head of a long file."""
    marker = "HEAD"
    asyncio.run(
        workspace.write_file(
            SID, {"path": "long.txt", "content": marker + "y" * (workspace.READ_CHAR_CAP + 5000)}
        )
    )
    read = asyncio.run(workspace.read_file(SID, {"path": "long.txt"}))
    assert read["truncated"] is True
    assert read["chars"] == workspace.READ_CHAR_CAP
    assert len(read["content"]) == workspace.READ_CHAR_CAP
    assert read["content"].startswith(marker)


# ─────────────────────────────────────────────────────────────────────────
# Deletion guard
# ─────────────────────────────────────────────────────────────────────────


def test_delete_refuses_directories_and_missing_files(ws):
    asyncio.run(workspace.write_file(SID, {"path": "sub/f.txt", "content": "x"}))
    out = asyncio.run(workspace.delete_file(SID, {"path": "sub"}))
    assert out["ok"] is False and "directory" in out["error"]
    assert (_root(ws) / "sub" / "f.txt").exists(), "directory untouched"

    missing = asyncio.run(workspace.delete_file(SID, {"path": "ghost.txt"}))
    assert missing["ok"] is False and "no such file" in missing["error"]


# ─────────────────────────────────────────────────────────────────────────
# Containment: relative-only, no '..', sensitive names, realpath
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tool,args",
    [
        ("read_file", {"path": "../other-session/file.md"}),
        ("read_file", {"path": "C:\\Windows\\evil.txt"}),
        ("read_file", {"path": "/etc/passwd"}),
        ("write_file", {"path": "..\\escape.txt", "content": "x"}),
        ("write_file", {"path": "a/../b.txt", "content": "x"}),
        ("delete_file", {"path": "../7/notes.md"}),
        ("list_files", {"subdir": ".."}),
    ],
)
def test_path_containment_denials(ws, tool, args):
    """Absolute paths and '..' segments never resolve — every tool funnels
    through resolve_session_path."""
    fn = getattr(workspace, tool)
    out = asyncio.run(fn(SID, args))
    assert out["ok"] is False
    assert "session folder" in out["error"] or "'..'" in out["error"]


@pytest.mark.parametrize(
    "tool,args",
    [
        ("write_file", {"path": "calliope_config.json", "content": "{}"}),
        ("write_file", {"path": ".env", "content": "KEY=1"}),
        ("read_file", {"path": "creds/id_rsa"}),
        ("read_file", {"path": "secret.txt"}),
        ("delete_file", {"path": "cert.p12"}),
        ("list_files", {"subdir": "credentials"}),
    ],
)
def test_sensitive_names_denied(ws, tool, args):
    """The shell's sensitive-name deny-list applies to scratch files too —
    config, db, .env, credentials, keys, certificates."""
    fn = getattr(workspace, tool)
    out = asyncio.run(fn(SID, args))
    assert out["ok"] is False
    assert "sensitive file" in out["error"]


def test_symlink_escape_refused(ws, tmp_path):
    """A symlink inside the session folder pointing outside must be refused:
    the realpath containment (skills_store pattern) fails the read and the
    delete, and the target file survives untouched."""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    root = _root(ws)
    root.mkdir(parents=True)
    try:
        (root / "leak.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable on this filesystem (win32 without privilege)")
    out = asyncio.run(workspace.read_file(SID, {"path": "leak.txt"}))
    assert out["ok"] is False and "escapes the session folder" in out["error"]
    gone = asyncio.run(workspace.delete_file(SID, {"path": "leak.txt"}))
    assert gone["ok"] is False
    assert outside.exists()


def test_sessions_are_isolated(ws):
    asyncio.run(workspace.write_file(SID, {"path": "a.md", "content": "mine"}))
    listed = asyncio.run(workspace.list_files(8, {}))
    assert listed["entries"] == []
    # the crossing attempt dies on the '..' guard, not on politeness
    crossed = asyncio.run(workspace.read_file(8, {"path": "../7/a.md"}))
    assert crossed["ok"] is False and "'..'" in crossed["error"]


def test_resolve_session_path_contract(ws):
    path, err = workspace.resolve_session_path(SID, "notes/draft.md")
    assert err is None
    assert path == (_root(ws) / "notes" / "draft.md").resolve()

    p2, err2 = workspace.resolve_session_path(SID, "", required=True)
    assert p2 is None and err2 == "path is required"

    # list_files' subdir: an empty value resolves to the folder root
    p3, err3 = workspace.resolve_session_path(SID, None, required=False)
    assert err3 is None
    assert p3.resolve() == _root(ws).resolve()

    # quoted paths from LLM payloads are unwrapped
    p4, err4 = workspace.resolve_session_path(SID, '"draft.md"')
    assert err4 is None and p4.name == "draft.md"


# ─────────────────────────────────────────────────────────────────────────
# Prompt section
# ─────────────────────────────────────────────────────────────────────────


def test_files_prompt_text_for_chat_origin(ws):
    chat = workspace.files_prompt_text(SID, origin="chat")
    assert chat is not None
    assert "SCRATCH WORKSPACE" in chat
    assert str(workspace.session_root(SID)) in chat
    assert "write_file" in chat
    assert "add_scene" in chat  # teaches the commit path
    # chat is the default origin
    assert workspace.files_prompt_text(SID) == chat


def test_files_prompt_text_hidden_on_build_scene(ws):
    """origin='scene' is the shot-only Build Surface — advertising file tools
    there would invite 'tool doesn't exist' loops."""
    assert workspace.files_prompt_text(SID, origin="scene") is None
