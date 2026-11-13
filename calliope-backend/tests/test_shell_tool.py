"""Agent shell (run_command): containment, approvals, exec mechanics.

The shell lives in ``calliope/agent/capabilities/shell.py`` (the old plugin
harness/registry is gone; ``catalog.execute("run_command", ...)`` routes here
and the tool works in unlinked sandbox chats). Every branch below is a
security contract:

- default OFF (``settings.agent_shell_enabled``) — the denial names Settings,
  teaching where to enable it;
- ``command`` must be a bare executable name; shell interpreters (bash /
  powershell / cmd / pwsh) are blocked outright because a single opaque
  payload defeats argv-level path containment;
- read-only inspection (whoami / where / which / a bare ``--version`` flag
  list) auto-runs; anything else needs the user's explicit ask — a
  ``scope="shell"`` question card (``log.has_structured_approval``) or the
  command named in their latest visible message (``shell.is_shell_request``),
  with the machine ``[Calliope context]`` appendix invisible to the check;
- ``cwd`` must resolve inside ``settings.workspace_dir``; path-like args must
  resolve inside workspace+assets_dir; sensitive names (calliope_config.json,
  calliope.db, .env, credentials, id_rsa, *.pem/.pfx/.p12) are always denied;
- exec is an argv list via ``create_subprocess_exec`` (never a shell string)
  with a stripped environment (KEY/SECRET/TOKEN/CREDENTIAL/PASSWORD), output
  capped at ~2000 chars per stream, and a wall-clock timeout that kills the
  process (``timed_out: true``).

Windows note: the suite runs on win32 — binaries are probed with
``shutil.which`` and the dependent tests skip when genuinely absent.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from calliope.agent.capabilities import shell
from calliope.agent.harness import log as session_log
from calliope.config import settings
from calliope.db import get_db, migrate_db

requires_python = pytest.mark.skipif(
    shutil.which("python") is None, reason="python is not on PATH"
)
requires_where = pytest.mark.skipif(
    shutil.which("where") is None, reason="where is not on PATH"
)
requires_whoami = pytest.mark.skipif(
    shutil.which("whoami") is None, reason="whoami is not on PATH"
)


@pytest.fixture()
def board(tmp_path, monkeypatch):
    """Temp data_dir/workspace/assets + a fresh DB. Shell starts DISABLED."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "assets_dir", tmp_path / "assets")
    monkeypatch.setattr(settings, "agent_workspace_dir", None)
    monkeypatch.setattr(settings, "agent_shell_enabled", False)
    asyncio.run(migrate_db(settings.db_path))
    settings.ensure_storage_dirs()
    return tmp_path


@pytest.fixture()
def enabled(board, monkeypatch):
    """The shell switched ON for the guard/exec tests."""
    monkeypatch.setattr(settings, "agent_shell_enabled", True)
    return board


def _session() -> int:
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO agent_sessions (title, project_id, status, origin) "
            "VALUES ('shell-test', NULL, 'idle', 'chat')"
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _say(session_id: int, text: str) -> None:
    session_log.append_event(session_id, "user/message", {"content": text})


def _approve(session_id: int, answer: str = "Yes, run it") -> None:
    """A scope='shell' question card answered by the user (the ask_user flow).

    The answered marker carries the asked event's seq — that is what
    ``has_structured_approval`` matches on."""
    seq = session_log.append_event(
        session_id, session_log.QUESTION_ASKED, {"question": "Run it?", "scope": "shell"}
    )
    session_log.append_event(
        session_id,
        session_log.QUESTION_ANSWERED,
        {"question_seq": seq, "answer": answer},
    )


def _run(session_id: int | None, args: dict) -> dict:
    return asyncio.run(shell.run_command(session_id, args))


# ─────────────────────────────────────────────────────────────────────────
# Default-off gate
# ─────────────────────────────────────────────────────────────────────────


def test_disabled_by_default_denial_names_settings(board):
    """The shell is opt-in: even a read-only command is refused, and the
    denial teaches where the toggle lives (never 'tool does not exist')."""
    out = _run(None, {"command": "whoami"})
    assert out["ok"] is False
    assert out["reason_code"] == "guard_shell_denied"
    assert "Settings" in out["error"]


def test_disabled_denial_precedes_every_other_check(board):
    """Order contract: the enabled gate is first, so a hostile command gets
    the enable guidance even when it would also fail the arg scan."""
    out = _run(None, {"command": "where", "args": ["calliope_config.json"]})
    assert out["ok"] is False
    assert "Settings" in out["error"]


# ─────────────────────────────────────────────────────────────────────────
# Command validation
# ─────────────────────────────────────────────────────────────────────────


def test_command_must_be_a_bare_name(enabled):
    """Paths and shell strings are refused — argv containment needs a bare
    executable name resolved via shutil.which."""
    for bad in ("C:/evil/python.exe", "..\\python", "/usr/bin/env", "tools/ffprobe"):
        out = _run(None, {"command": bad})
        assert out["ok"] is False, bad
        assert "bare executable name" in out["error"], bad


def test_shell_interpreters_blocked_outright(enabled):
    """sh/powershell/cmd payloads defeat argv-level path containment, so the
    interpreters are denied before they are even looked up on PATH."""
    for name in ("bash", "zsh", "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh"):
        out = _run(None, {"command": name, "args": ["-c", "echo pwned"]})
        assert out["ok"] is False, name
        assert "interpreter" in out["error"], name


def test_unknown_binary_denied(enabled):
    out = _run(None, {"command": "definitely-not-installed-xyz-913"})
    assert out["ok"] is False
    assert "not an installed executable" in out["error"]


# ─────────────────────────────────────────────────────────────────────────
# Read-only auto-run vs HITL approval
# ─────────────────────────────────────────────────────────────────────────


@requires_whoami
def test_read_only_inspection_runs_without_approval(enabled):
    """whoami/where/which (and bare --version lists) cannot mutate anything —
    they run with no session, no card, and no prose ask."""
    out = _run(None, {"command": "whoami"})
    assert out["ok"] is True, out
    assert out["stdout"].strip()


@requires_python
def test_version_flags_count_as_read_only(enabled):
    """python --version is a version-flag-only arg list → auto-run."""
    out = _run(None, {"command": "python", "args": ["--version"]})
    assert out["ok"] is True, out
    assert out["exit_code"] == 0


@requires_python
def test_mutating_command_denied_without_user_ask(enabled):
    """A benign user message that never names the command or a shell verb is
    NOT permission — the denial names the ask_user card that unlocks it."""
    sid = _session()
    _say(sid, "tidy up my scratch folder")
    out = _run(sid, {"command": "python", "args": ["-c", "print('hi')"]})
    assert out["ok"] is False
    assert out["reason_code"] == "guard_shell_denied"
    assert "ask_user" in out["error"]
    assert 'scope="shell"' in out["error"]


@requires_python
def test_no_session_means_no_prose_approval(enabled):
    """Without a session there is no latest user message — only a structured
    card (which also needs one) could unlock, so the call is denied."""
    out = _run(None, {"command": "python", "args": ["-c", "print('x')"]})
    assert out["ok"] is False
    assert "ask_user" in out["error"]


@requires_python
def test_structured_card_unlocks(enabled):
    """The ask_user flow: a scope='shell' card answered affirmatively lets the
    retry through — proven without any user/message naming the command."""
    sid = _session()
    _approve(sid)
    out = _run(sid, {"command": "python", "args": ["-c", "print('card-approved')"]})
    assert out["ok"] is True, out
    assert "card-approved" in out["stdout"]


@requires_python
def test_refusal_answer_keeps_denial(enabled):
    """A refusal option ("No") does NOT count as approval."""
    sid = _session()
    _approve(sid, "No")
    out = _run(sid, {"command": "python", "args": ["-c", "print('nope')"]})
    assert out["ok"] is False
    assert "ask_user" in out["error"]


@requires_python
def test_new_unanswered_card_voids_previous_yes(enabled):
    """Each new card of the scope resets the verdict — a stale 'yes' from an
    older card must not approve a fresh ask."""
    sid = _session()
    _approve(sid)
    session_log.append_event(
        sid, session_log.QUESTION_ASKED, {"question": "Again?", "scope": "shell"}
    )
    out = _run(sid, {"command": "python", "args": ["-c", "print('x')"]})
    assert out["ok"] is False


@requires_python
def test_other_scope_card_does_not_unlock_shell(enabled):
    """Only scope='shell' cards grant shell — an answered 'info' card grants
    nothing."""
    sid = _session()
    seq = session_log.append_event(
        sid, session_log.QUESTION_ASKED, {"question": "Pick one?", "scope": "info"}
    )
    session_log.append_event(
        sid,
        session_log.QUESTION_ANSWERED,
        {"question_seq": seq, "answer": "Yes, run it"},
    )
    out = _run(sid, {"command": "python", "args": ["-c", "print('x')"]})
    assert out["ok"] is False


@requires_python
def test_command_named_in_user_words_unlocks(enabled):
    """The command appearing in the user's own latest message is permission."""
    sid = _session()
    _say(sid, "please run python to summarize the scratch files")
    out = _run(sid, {"command": "python", "args": ["-c", "print('prose-approved')"]})
    assert out["ok"] is True, out
    assert "prose-approved" in out["stdout"]


@requires_python
def test_negated_command_in_prose_stays_denied(enabled):
    """Clause-scoped negation: 'do not run python' is a veto, not a request."""
    sid = _session()
    _say(sid, "do not run python for this")
    out = _run(sid, {"command": "python", "args": ["-c", "print('x')"]})
    assert out["ok"] is False


@requires_python
def test_appendix_never_grants_shell(enabled):
    """The machine ``[Calliope context]`` appendix can carry a command name —
    it is stripped before the prose check, so it can never auto-approve."""
    sid = _session()
    _say(sid, "tidy the workspace\n\n[Calliope context]\npython=3.12 mentioned")
    out = _run(sid, {"command": "python", "args": ["-c", "print('x')"]})
    assert out["ok"] is False


def test_user_allows_shell_reads_only_user_messages(enabled):
    """user_allows_shell: structured card or the latest user/message only —
    assistant replies and tool results never grant shell."""
    sid = _session()
    _say(sid, "please organize my drafts")
    session_log.append_event(
        sid, "assistant/message", {"content": "run python to organize?"}
    )
    assert shell.user_allows_shell(sid, "python") is False
    _say(sid, "ok, run python for me")
    assert shell.user_allows_shell(sid, "python") is True
    # a different command in the same message is not permission for python
    assert shell.user_allows_shell(sid, "ffmpeg") is False
    assert shell.user_allows_shell(None, "python") is False


def test_is_shell_request_cues():
    """Generic run/execute-a-command cues, clause-scoped negation."""
    assert shell.is_shell_request("run ffmpeg over the raw clip", None) is True
    assert shell.is_shell_request("please execute this script", None) is True
    assert shell.is_shell_request("do not run ffmpeg tonight", None) is False
    # a terminal mention without a run verb is not a request
    assert shell.is_shell_request("I want to buy a terminal", None) is False


def test_is_shell_request_command_named():
    """With a command, the name in visible prose counts by itself (documented
    design choice), and the [Calliope context] appendix is invisible."""
    assert shell.is_shell_request("run python to check the files", "python") is True
    assert shell.is_shell_request("never run python here", "python") is False
    assert shell.is_shell_request("tidy my drafts", "python") is False
    assert shell.is_shell_request("", "python") is False
    assert shell.is_shell_request("python would be handy here", "python") is True
    assert shell.is_shell_request("[Calliope context]\npython=3.12", "python") is False


# ─────────────────────────────────────────────────────────────────────────
# Containment: cwd + path-like args
# ─────────────────────────────────────────────────────────────────────────


@requires_whoami
def test_cwd_outside_workspace_denied(enabled):
    """The process is born inside the workspace or nowhere — the user's real
    home is definitively outside the test workspace."""
    out = _run(None, {"command": "whoami", "cwd": str(Path.home())})
    assert out["ok"] is False
    assert "outside the agent workspace" in out["error"]


@requires_whoami
def test_cwd_inside_workspace_allowed(enabled):
    sub = settings.workspace_dir / "sub"
    sub.mkdir(parents=True, exist_ok=True)
    out = _run(None, {"command": "whoami", "cwd": str(sub)})
    assert out["ok"] is True, out
    assert Path(out["cwd"]) == sub.resolve()


@requires_where
def test_sensitive_args_denied(enabled):
    """Sensitive names are denied before any containment logic — even on a
    read-only command, even as bare (non-path) words."""
    for arg in (
        "calliope_config.json",
        "calliope.db",
        ".env",
        "creds/id_rsa",
        "apikey.pem",
        "backup.p12",
        "My-SECRET.txt",
    ):
        out = _run(None, {"command": "where", "args": [arg]})
        assert out["ok"] is False, arg
        assert "sensitive file" in out["error"], arg


@requires_where
def test_absolute_path_arg_outside_denied(enabled, tmp_path):
    """tmp_path IS the test data_dir — its own root is outside both the
    workspace (data_dir/workspace) and the assets folder."""
    outside = tmp_path / "movie.mp4"
    outside.write_bytes(b"xx")
    out = _run(None, {"command": "where", "args": [str(outside)]})
    assert out["ok"] is False
    assert "outside the agent workspace" in out["error"]


@requires_where
def test_args_inside_workspace_and_assets_pass_the_guard(enabled):
    """assets_dir is a legit read root for reference inspection; the workspace
    is the write root. The guard passes both through (where itself may or may
    not match the name — that is an exit code, not a denial)."""
    ref = settings.assets_dir / "ref.mp4"
    ref.write_bytes(b"xx")
    scratch = settings.workspace_dir / "draft.txt"
    scratch.write_text("x", encoding="utf-8")
    for arg in (str(ref), str(scratch)):
        out = _run(None, {"command": "where", "args": [arg]})
        assert out.get("reason_code") is None, out
        assert "outside the agent workspace" not in str(out.get("error", ""))


# ─────────────────────────────────────────────────────────────────────────
# Exec mechanics
# ─────────────────────────────────────────────────────────────────────────


@requires_python
def test_env_stripped_from_child(enabled, monkeypatch):
    """KEY/SECRET/TOKEN/CREDENTIAL/PASSWORD env vars are stripped from the
    child process; ordinary vars pass through (the env is filtered, not
    emptied). The probe is a script file in the workspace so the -sensitive
    arg scan never sees the variable name."""
    script = settings.workspace_dir / "env_check.py"
    script.write_text(
        "import os\n"
        "print('stripped:', [k for k in os.environ if k.startswith('CALLIOPE_TEST_SECRET')])\n"
        "print('plain:', os.environ.get('CALLIOPE_TEST_PLAIN'))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CALLIOPE_TEST_SECRET_TOKEN", "topsecret-value")
    monkeypatch.setenv("CALLIOPE_TEST_PLAIN", "survives")
    sid = _session()
    _approve(sid)
    out = _run(sid, {"command": "python", "args": [str(script)]})
    assert out["ok"] is True, out
    assert "stripped: []" in out["stdout"]
    assert "plain: survives" in out["stdout"]
    assert "topsecret" not in out["stdout"]


@requires_python
def test_timeout_kills_the_process(enabled):
    """A command over its wall-clock budget is killed and reported with
    timed_out=True — the wait never hangs past min(timeout_sec, queue cap)."""
    sid = _session()
    _approve(sid)
    out = _run(
        sid,
        {
            "command": "python",
            "args": ["-c", "import time; time.sleep(30)"],
            "timeout_sec": 1,
        },
    )
    assert out["ok"] is False
    assert out.get("timed_out") is True
    assert "exceeded" in out["error"]


@requires_python
def test_nonzero_exit_reported(enabled):
    sid = _session()
    _approve(sid)
    out = _run(sid, {"command": "python", "args": ["-c", "raise SystemExit(3)"]})
    assert out["ok"] is False
    assert out["exit_code"] == 3


@requires_python
def test_stdout_captured_and_capped(enabled):
    """Streams are captured but capped (~2000 chars) with an explicit
    stdout_truncated marker — a chatty command cannot flood the context."""
    sid = _session()
    _approve(sid)
    out = _run(sid, {"command": "python", "args": ["-c", "print('x' * 5000)"]})
    assert out["ok"] is True, out
    assert out.get("stdout_truncated") is True
    assert len(out["stdout"]) <= 2000
