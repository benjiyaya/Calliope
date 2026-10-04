"""Agent shell (run_command): containment, approvals, exec mechanics.

The shell is default-OFF; every guard branch here is a security contract:
- disabled → guard_shell_disabled
- read-only (ffprobe / --version) runs without approval, anything else
  needs a scope='shell' card or the command named in the user's words
- cwd must resolve inside the agent workspace
- path-like args must resolve inside workspace/assets_dir, and sensitive
  names (config, *.db, .env, credentials) are always denied
- shell interpreters are blocked outright
- exec is argv-list based with a stripped environment and a timeout
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from calliope.agent.harness import build_harness
from calliope.agent.harness import log as session_log
from calliope.agent.harness.registry import ToolContext

PYTHON = Path(sys.executable).stem  # bare name ("python") — the tool refuses paths


@pytest.fixture(autouse=True, scope="module")
def _never_touch_real_db():
    """Redirect data_dir before ANY write in this module (repo test rule)."""
    import tempfile

    import calliope.config as config_module
    from calliope.db import migrate_db

    prev = {
        "data_dir": config_module.settings.data_dir,
        "assets_dir": config_module.settings.assets_dir,
        "agent_workspace_dir": config_module.settings.agent_workspace_dir,
        "agent_shell_enabled": config_module.settings.agent_shell_enabled,
        "dry_run": config_module.settings.dry_run,
    }
    with tempfile.TemporaryDirectory() as tmp:
        config_module.settings.data_dir = Path(tmp)
        config_module.settings.assets_dir = Path(tmp) / "assets"
        config_module.settings.agent_workspace_dir = None
        config_module.settings.agent_shell_enabled = False
        config_module.settings.dry_run = True
        config_module.settings.ensure_storage_dirs()
        asyncio.run(migrate_db(config_module.settings.db_path))
        yield
    for k, v in prev.items():
        setattr(config_module.settings, k, v)


def _session() -> int:
    from calliope.db import get_db
    from calliope.config import settings

    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO agent_sessions (title, project_id) VALUES (?, NULL)",
            ("shell-test",),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _say(session_id: int, text: str) -> None:
    session_log.append_event(session_id, session_log.USER_MESSAGE, {"content": text})


def _answer_card(session_id: int, answer: str) -> None:
    """A scope='shell' question card answered affirmatively (the ask_user flow)."""
    session_log.append_event(
        session_id, session_log.QUESTION_ASKED, {"question": "Run it?", "scope": "shell"}
    )
    session_log.append_event(session_id, session_log.QUESTION_ANSWERED, {"scope": "shell", "answer": answer})
    session_log.append_event(session_id, session_log.USER_MESSAGE, {"content": answer})


@pytest.fixture()
def enabled():
    import calliope.config as config_module

    config_module.settings.agent_shell_enabled = True
    yield
    config_module.settings.agent_shell_enabled = False


def _ctx() -> ToolContext:
    return ToolContext(session_id=_session(), project_id=None)


# ─────────────────────────────────────────────────────────────────────────
# Registration & default-off
# ─────────────────────────────────────────────────────────────────────────


def test_run_command_registered_and_sandbox_visible():
    registry, _ = build_harness()
    t = registry.get("run_command")
    assert t is not None
    assert t.requires_project is False
    assert t.category == "system"


def test_disabled_by_default_denial(client):
    registry, _ = build_harness()
    out = asyncio.run(registry.execute(_ctx(), "run_command", {"command": "ffprobe"}))
    assert out["ok"] is False
    assert out["reason_code"] == "guard_shell_disabled"


# ─────────────────────────────────────────────────────────────────────────
# Command validation
# ─────────────────────────────────────────────────────────────────────────


def test_path_in_command_denied(enabled):
    registry, _ = build_harness()
    out = asyncio.run(
        registry.execute(_ctx(), "run_command", {"command": "C:/evil/python.exe"})
    )
    assert out["reason_code"] == "guard_shell_denied_command"


def test_shell_interpreter_denied(enabled):
    registry, _ = build_harness()
    out = asyncio.run(
        registry.execute(_ctx(), "run_command", {"command": "bash", "args": ["-c", "ls"]})
    )
    assert out["reason_code"] == "guard_shell_denied_command"


def test_unknown_binary_denied(enabled):
    registry, _ = build_harness()
    out = asyncio.run(
        registry.execute(_ctx(), "run_command", {"command": "definitely-not-installed-xyz"})
    )
    assert out["reason_code"] == "guard_shell_denied_command"


# ─────────────────────────────────────────────────────────────────────────
# Approval gate
# ─────────────────────────────────────────────────────────────────────────


def test_read_only_probe_runs_without_approval(enabled):
    """A --version flag pair counts as read-only — no user ask needed."""
    registry, _ = build_harness()
    out = asyncio.run(
        registry.execute(
            _ctx(), "run_command", {"command": PYTHON, "args": ["--version"]}
        )
    )
    assert out["ok"] is True, out
    assert out["exit_code"] == 0


def test_mutating_without_approval_denied(enabled):
    registry, _ = build_harness()
    sid = _session()
    _say(sid, "organize my workspace please")  # no command named, no shell cue
    out = asyncio.run(
        registry.execute(
            ToolContext(session_id=sid, project_id=None),
            "run_command",
            {"command": PYTHON, "args": ["-c", "print('hi')"]},
        )
    )
    assert out["ok"] is False
    assert out["reason_code"] == "guard_shell_approval"


def test_command_named_in_user_words_allows(enabled):
    registry, _ = build_harness()
    sid = _session()
    _say(sid, "run python -c to check the workspace files")
    out = asyncio.run(
        registry.execute(
            ToolContext(session_id=sid, project_id=None),
            "run_command",
            {"command": "python", "args": ["-c", "print('approved')"]},
        )
    )
    assert out["ok"] is True, out


def test_structured_shell_approval_allows(enabled):
    registry, _ = build_harness()
    sid = _session()
    _answer_card(sid, "Yes, run it")
    out = asyncio.run(
        registry.execute(
            ToolContext(session_id=sid, project_id=None),
            "run_command",
            {"command": PYTHON, "args": ["-c", "print('card-approved')"]},
        )
    )
    assert out["ok"] is True, out


# ─────────────────────────────────────────────────────────────────────────
# Containment
# ─────────────────────────────────────────────────────────────────────────


def test_cwd_outside_workspace_denied(enabled):
    registry, _ = build_harness()
    out = asyncio.run(
        registry.execute(
            _ctx(),
            "run_command",
            {"command": "ffprobe", "cwd": str(Path(settings_data_dir_home()))},
        )
    )
    assert out["reason_code"] == "guard_shell_outside_workspace"


def settings_data_dir_home() -> Path:
    """The user's real home — definitively outside the test workspace."""
    return Path.home()


def test_sensitive_arg_denied(enabled, tmp_path):
    registry, _ = build_harness()
    secret = tmp_path / "calliope_config.json"
    secret.write_text("{}", encoding="utf-8")
    out = asyncio.run(
        registry.execute(
            _ctx(),
            "run_command",
            {"command": "ffprobe", "args": [str(secret)]},
        )
    )
    assert out["reason_code"] == "guard_shell_denied_path"


def test_db_arg_denied(enabled, tmp_path):
    registry, _ = build_harness()
    db = tmp_path / "calliope.db"
    db.write_bytes(b"")
    out = asyncio.run(
        registry.execute(
            _ctx(),
            "run_command",
            {"command": "ffprobe", "args": [str(db)]},
        )
    )
    assert out["reason_code"] == "guard_shell_denied_path"


def test_env_file_arg_denied(enabled):
    registry, _ = build_harness()
    out = asyncio.run(
        registry.execute(
            _ctx(),
            "run_command",
            {"command": "ffprobe", "args": ["--", ".env"]},
        )
    )
    assert out["reason_code"] == "guard_shell_denied_path"


def test_outside_absolute_arg_denied(enabled, tmp_path):
    registry, _ = build_harness()
    outside = tmp_path / "movie.mp4"
    outside.write_bytes(b"00")
    out = asyncio.run(
        registry.execute(
            _ctx(),
            "run_command",
            {"command": "ffprobe", "args": [str(outside)]},
        )
    )
    assert out["reason_code"] == "guard_shell_outside_workspace"


def test_inside_asset_arg_allowed(enabled):
    """assets_dir is a legit read root for reference inspection."""
    from calliope.config import settings

    registry, _ = build_harness()
    ref = settings.assets_dir / "ref.mp4"
    ref.write_bytes(b"00")
    out = asyncio.run(
        registry.execute(
            _ctx(),
            "run_command",
            {"command": "ffprobe", "args": ["-v", "error", str(ref)]},
        )
    )
    # Not a real mp4 — ffprobe fails, but the GUARD must not have denied it.
    assert out.get("reason_code") is None
    assert out.get("exit_code") != 0 or out.get("ok") is False or out["ok"] is True


def test_workspace_relative_cwd_allowed(enabled, tmp_path):
    from calliope.config import settings

    registry, _ = build_harness()
    sub = settings.workspace_dir / "sub"
    sub.mkdir(parents=True, exist_ok=True)
    sid = _session()
    _say(sid, "run python to print the cwd")
    out = asyncio.run(
        registry.execute(
            ToolContext(session_id=sid, project_id=None),
            "run_command",
            {"command": PYTHON, "args": ["-c", "import os; print(os.getcwd())"], "cwd": str(sub)},
        )
    )
    assert out["ok"] is True, out
    assert out["stdout"].strip().lower() == str(sub.resolve()).lower()


# ─────────────────────────────────────────────────────────────────────────
# Exec mechanics
# ─────────────────────────────────────────────────────────────────────────


def test_stdout_captured_and_env_stripped(enabled):
    registry, _ = build_harness()
    sid = _session()
    _say(sid, "run python -c to dump env keys")
    out = asyncio.run(
        registry.execute(
            ToolContext(session_id=sid, project_id=None),
            "run_command",
            {
                "command": PYTHON,
                "args": [
                    "-c",
                    "import os; print('KEY' + '=' + str('LLM_API_KEY' in os.environ or 'FAKE_API_KEY' in os.environ)); print('ran')",
                ],
            },
        )
    )
    assert out["ok"] is True, out
    assert "ran" in out["stdout"]
    assert "=False" in out["stdout"]


def test_timeout_kills_process(enabled):
    registry, _ = build_harness()
    sid = _session()
    _say(sid, "run python -c sleep check")
    out = asyncio.run(
        registry.execute(
            ToolContext(session_id=sid, project_id=None),
            "run_command",
            {
                "command": PYTHON,
                "args": ["-c", "import time; time.sleep(30)"],
                "timeout_sec": 2,
            },
        )
    )
    assert out["ok"] is False
    assert out.get("timed_out") is True


def test_nonzero_exit_reported(enabled):
    registry, _ = build_harness()
    sid = _session()
    _say(sid, "run python -c failing check")
    out = asyncio.run(
        registry.execute(
            ToolContext(session_id=sid, project_id=None),
            "run_command",
            {"command": PYTHON, "args": ["-c", "raise SystemExit(3)"]},
        )
    )
    assert out["ok"] is False
    assert out["exit_code"] == 3


# ─────────────────────────────────────────────────────────────────────────
# Settings roundtrip
# ─────────────────────────────────────────────────────────────────────────


def test_settings_roundtrip_for_new_keys(client, monkeypatch):
    """POST /api/settings persists agent_workspace_dir + agent_shell_enabled."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        r = client.post(
            "/api/settings",
            json={"agent_workspace_dir": tmp, "agent_shell_enabled": True},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["agent_shell_enabled"] is True
        assert Path(body["agent_workspace_dir"]).resolve() == Path(tmp).resolve()

        r2 = client.post("/api/settings", json={"agent_shell_enabled": False})
        assert r2.status_code == 200
        assert r2.json()["agent_shell_enabled"] is False


def test_settings_rejects_ephemeral_workspace():
    """load/save poison-guard: a %TEMP% workspace resets to the data_dir default."""
    import tempfile

    import calliope.config as config_module

    s = config_module.settings
    prev = s.agent_workspace_dir
    try:
        with tempfile.TemporaryDirectory() as tmp:
            s.agent_workspace_dir = Path(tmp)
            s.save_config_file()
            assert s.agent_workspace_dir is None
            assert s.workspace_dir == s.data_dir / "workspace"
    finally:
        # This test drives the REAL save path (conftest redirects CONFIG_FILE to a
        # scratch file, so no operator data is at risk), but it must still not
        # leave the shared singleton holding a reset workspace for later tests.
        s.agent_workspace_dir = prev
