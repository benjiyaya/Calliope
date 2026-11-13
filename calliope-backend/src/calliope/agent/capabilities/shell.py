"""The agent shell (run_command).

Cursor/Hermes-class agents are flexible because the model can run commands,
not only curated tools. Calliope ships to end users with secrets on disk
(``calliope_config.json`` carries the LLM API key; ``calliope.db`` carries
every session), so the shell is sandboxed to a dedicated workspace folder:

- default OFF (``settings.agent_shell_enabled``) — explicit opt-in,
- cwd AND path-like args containment-checked against
  ``settings.workspace_dir`` (``data_dir / "workspace"`` unless overridden)
  plus ``assets_dir`` for read-side reference inspection,
- deny-list on sensitive names (config / *.db / .env / credentials),
- shell interpreters blocked (a ``bash -c`` payload defeats argv-level
  path containment),
- non-read-only commands need the user's explicit ask: a structured
  ``scope="shell"`` question card (has_structured_approval) or the command
  named in their visible prose (is_shell_request),
- exec is always an argv list via ``create_subprocess_exec`` (never a
  shell string), with a stripped environment and a wall-clock timeout.

Residual risk (accepted, documented in AGENTS.md): an approved interpreter
(``python -c …``) can compute paths dynamically — the guard chain is
defense-in-depth + a full audit trail (tool/result events), not a VM
boundary. Every denial is actionable: it names the setting, the file, or
the ask_user card that unlocks the retry.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

from calliope.config import settings

logger = logging.getLogger("calliope.agent.capabilities.shell")

DEFAULT_TIMEOUT_SEC = 120.0
_STREAM_CAP = 2000
_ARG_CAP = 64

# Commands whose payload is a single opaque string — they defeat argv-level
# path containment, so they are refused outright.
_SHELL_INTERPRETERS = frozenset(
    {"sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "cmd", "cmd.exe",
     "powershell", "powershell.exe", "pwsh", "pwsh.exe"}
)

# Names that must never appear in any argument, whatever the command.
_SENSITIVE_NAME_RE = re.compile(
    r"calliope_config\.json|\.env\b|calliope\.db|credential|secret|id_rsa|"
    r"\.pem\b|\.pfx\b|\.p12\b",
    re.IGNORECASE,
)

# Env var names stripped from the child process (case-insensitive contains).
_ENV_STRIP_RE = re.compile(r"KEY|SECRET|TOKEN|CREDENTIAL|PASSWORD|PASSWD", re.IGNORECASE)

# A command that cannot mutate anything may auto-run without approval.
_VERSION_FLAGS = frozenset({"--version", "-version", "-V", "--help", "-h", "-?"})

# Shell-verb prose cues (is_shell_request) — clause-scoped negation applies.
_SHELL_REQUEST_RE = re.compile(
    r"\b(run|execute|launch|invoke)\b.{0,40}?\b"
    r"(command|shell|terminal|ffmpeg|ffprobe|script|python|cli)\b",
    re.IGNORECASE,
)
_NEGATE_RE = re.compile(
    r"\b(no|not|don'?t|do\s+not|cancel|stop|never|abort|skip|hold\s+on|wait)\b",
    re.IGNORECASE,
)
_APPENDIX_MARK = "[Calliope context]"


# ─────────────────────────────────────────────────────────────────────────
# Approval (structured card or visible prose)
# ─────────────────────────────────────────────────────────────────────────


def _user_prose(text: str) -> str:
    """Visible user words only — drop the machine `[Calliope context]`
    appendix (it can carry a command name and would otherwise auto-approve)."""
    raw = text or ""
    if _APPENDIX_MARK in raw:
        raw = raw.split(_APPENDIX_MARK, 1)[0]
    return raw.strip()


def _cue_is_negated(before: str, after: str) -> bool:
    """Only a short negation directly adjacent to the cue vetoes it."""
    before_words = before.split()
    if before_words and _NEGATE_RE.search(" ".join(before_words[-4:])):
        return True
    if re.match(r"\s*(no|not|don'?t|never)\b", after, re.IGNORECASE):
        return True
    return False


def is_shell_request(text: str, command: str | None = None) -> bool:
    """True when the user's prose explicitly asks for shell/command use.

    With `command`, the command name appearing in the visible words counts
    by itself; without it, a generic run/execute-a-command cue is matched.
    Clause-scoped: the cue must not be negated ("do not run ffmpeg" is not
    a request, "do not overthink, run ffprobe on it" is).
    """
    t = _user_prose(text)
    if not t:
        return False
    clauses = re.split(r"[,.;!?]|\bbut\b|\bhowever\b", t, flags=re.IGNORECASE)
    if command:
        escaped = re.escape(command)
        if not re.search(rf"(?<![A-Za-z0-9_-]){escaped}(?![A-Za-z0-9_-])", t, re.IGNORECASE):
            return False
        for clause in clauses:
            m = re.search(
                rf"(?<![A-Za-z0-9_-]){escaped}(?![A-Za-z0-9_-])", clause, re.IGNORECASE
            )
            if m and not _cue_is_negated(clause[: m.start()], clause[m.end():]):
                return True
        return False
    for clause in clauses:
        m = _SHELL_REQUEST_RE.search(clause)
        if m and not _cue_is_negated(clause[: m.start()], clause[m.end():]):
            return True
    return False


def _latest_user_message(session_id: int | None) -> str:
    if session_id is None:
        return ""
    from calliope.agent.harness.log import read_events

    for event in reversed(read_events(session_id)):
        if event.type == "user/message":
            return str(event.data.get("content") or "")
    return ""


def user_allows_shell(session_id: int | None, command: str | None = None) -> bool:
    """Shell permission from explicit user acts only.

    A structured scope='shell' card answered affirmatively, or the command
    named in the user's own latest message. Steering notes and tool results
    never grant it.
    """
    from calliope.agent.harness.log import has_structured_approval

    if session_id is not None and has_structured_approval(session_id, "shell"):
        return True
    return is_shell_request(_latest_user_message(session_id), command)


# ─────────────────────────────────────────────────────────────────────────
# Guard chain (everything checked BEFORE spawning)
# ─────────────────────────────────────────────────────────────────────────


def _is_read_only(command: str, args: list[str]) -> bool:
    if command.lower() in {"ffprobe", "which", "where", "whoami", "hostname"}:
        return True
    return bool(args) and all(a in _VERSION_FLAGS for a in args)


def _looks_like_path(arg: str) -> bool:
    if not arg or len(arg) > 260:
        return False
    if "/" in arg or "\\" in arg:
        return True
    p = Path(arg)
    return bool(p.anchor) or p.is_file() or p.is_dir()


def _contained(path: Path, roots: list[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def _child_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not _ENV_STRIP_RE.search(k)}


def _guard(session_id: int | None, args: dict[str, Any]) -> str | None:
    """Return a denial message, or None when every check passed."""
    if not settings.agent_shell_enabled:
        return (
            "The agent shell is disabled. Enable it in Settings → Agent "
            "('Agent shell' toggle) — commands can then run only inside the "
            "agent workspace folder (Settings → System Paths)."
        )

    command = str(args.get("command") or "").strip()
    if not command or "/" in command or "\\" in command:
        return (
            "command must be a bare executable name (e.g. 'ffmpeg'), not a "
            "path or a shell string."
        )
    if command.lower() in _SHELL_INTERPRETERS:
        return (
            f"'{command}' is a shell interpreter and is blocked — its payload "
            "would bypass path containment. Call the real tool directly."
        )
    if shutil.which(command) is None:
        return f"'{command}' is not an installed executable on this machine."

    raw_args = args.get("args") or []
    if not isinstance(raw_args, list) or len(raw_args) > _ARG_CAP:
        return f"args must be a list of at most {_ARG_CAP} strings."
    str_args = [str(a) for a in raw_args]

    # Approval: read-only inspection auto-runs; anything else needs the
    # user's explicit ask (scope='shell' card, or the command in their words).
    if not _is_read_only(command, str_args) and not user_allows_shell(session_id, command):
        return (
            f"Running '{command}' is human-in-the-loop. Ask the user with "
            f"ask_user(question=\"Run '{command}' in the agent workspace?\", "
            "options=[\"Yes, run it\", \"No\"], scope=\"shell\") and retry "
            "after they answer — read-only inspection (ffprobe --version) "
            "does not need this."
        )

    # cwd containment: the process is born inside the workspace or nowhere.
    ws = settings.workspace_dir
    roots = [ws, settings.assets_dir]
    cwd_arg = args.get("cwd")
    if cwd_arg is not None:
        cwd_path = Path(str(cwd_arg))
        if not _contained(cwd_path, [ws]):
            return (
                f"cwd '{cwd_arg}' resolves outside the agent workspace ({ws}) "
                "— the shell cannot leave that folder."
            )
    for a in str_args:
        if _SENSITIVE_NAME_RE.search(a):
            return (
                f"Argument '{a}' names a sensitive file (config/database/"
                "credentials) — the agent shell can never touch those."
            )
        if not _looks_like_path(a):
            continue
        p = Path(a)
        if p.anchor or "/" in a or "\\" in a:
            if not _contained(p, roots):
                return (
                    f"Argument '{a}' resolves outside the agent workspace "
                    f"({ws}) and assets folder — pass a path inside them."
                )
        elif (p.is_file() or p.is_dir()) and not _contained(p, roots):
            return (
                f"Argument '{a}' resolves to an existing path outside the "
                f"agent workspace ({ws}) — pass a path inside it instead."
            )
    return None


# ─────────────────────────────────────────────────────────────────────────
# Executor
# ─────────────────────────────────────────────────────────────────────────


async def run_command(session_id: int | None, args: dict[str, Any]) -> dict[str, Any]:
    denial = _guard(session_id, args)
    if denial is not None:
        return {"ok": False, "error": denial, "reason_code": "guard_shell_denied"}

    command = str(args.get("command") or "").strip()
    str_args = [str(a) for a in (args.get("args") or [])]
    ws = settings.workspace_dir
    cwd_arg = args.get("cwd")
    cwd = Path(str(cwd_arg)).resolve() if cwd_arg else ws.resolve()

    timeout_arg = args.get("timeout_sec")
    try:
        timeout = float(timeout_arg) if timeout_arg is not None else DEFAULT_TIMEOUT_SEC
    except (TypeError, ValueError):
        return {"ok": False, "error": "timeout_sec must be a number"}
    cap = settings.queue_poll_timeout_sec
    timeout = max(1.0, min(timeout, cap) if cap > 0 else timeout)

    exe = shutil.which(command)
    if exe is None:
        return {"ok": False, "error": f"'{command}' is not installed"}

    creationflags = getattr(os, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = await asyncio.create_subprocess_exec(
            exe,
            *str_args,
            cwd=str(cwd),
            env=_child_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
    except OSError as exc:
        return {"ok": False, "error": f"failed to start '{command}': {exc}"}

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            # Reap the killed child so the transport is released now, not by
            # GC after the loop closed (Windows proactor warning otherwise).
            await proc.wait()
        except OSError:
            pass
        return {
            "ok": False,
            "error": (
                f"'{command}' exceeded {timeout:.0f}s and was killed. Narrow the "
                "task (fewer files, shorter work) or ask for a longer timeout."
            ),
            "timed_out": True,
        }

    stdout = stdout_b.decode("utf-8", errors="replace")[:_STREAM_CAP]
    stderr = stderr_b.decode("utf-8", errors="replace")[:_STREAM_CAP]
    out: dict[str, Any] = {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "cwd": str(cwd),
    }
    if stdout:
        out["stdout"] = stdout
        if len(stdout_b) > _STREAM_CAP:
            out["stdout_truncated"] = True
    if stderr:
        out["stderr"] = stderr
        if len(stderr_b) > _STREAM_CAP:
            out["stderr_truncated"] = True
    if proc.returncode != 0 and not stderr:
        out["error"] = f"'{command}' exited with code {proc.returncode}"
    return out
