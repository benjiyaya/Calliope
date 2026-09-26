"""System plugin: the agent shell (run_command).

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
  ``scope="shell"`` question card or the command named in their words,
- exec is always an argv list via ``create_subprocess_exec`` (never a
  shell string), with a stripped environment and a wall-clock timeout.

Residual risk (accepted, documented in AGENTS.md): an approved interpreter
(``python -c …``) can compute paths dynamically — the guard chain is
defense-in-depth + full audit (tool/call + tool/result events), not a VM
boundary. Policy lives in the pre-execute hook; the executor is mechanics.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import re
from pathlib import Path
from typing import Any

from calliope.agent.harness.registry import (
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    allow,
    deny,
)
from calliope.config import settings

logger = logging.getLogger("calliope.harness.plugins.system")

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


async def _shell_guard(ctx: ToolContext, t: ToolDefinition, args: dict) -> Any:
    """Pre-execute policy for run_command (everything the plan's guard chain
    checks BEFORE spawning). Every hook must return a decision — allow() for
    tools this guard does not own."""
    if t.name != "run_command":
        return allow()
    import calliope.agent.harness as harness

    if not settings.agent_shell_enabled:
        return deny(
            "The agent shell is disabled. Enable it in Settings → Agent "
            "('Agent shell' toggle) — commands can then run only inside the "
            "agent workspace folder (Settings → System Paths).",
            code=harness.GUARD_SHELL_DISABLED,
        )

    command = str(args.get("command") or "").strip()
    if not command or "/" in command or "\\" in command:
        return deny(
            "command must be a bare executable name (e.g. 'ffmpeg'), not a path "
            "or a shell string.",
            code=harness.GUARD_SHELL_DENIED_COMMAND,
        )
    if command.lower() in _SHELL_INTERPRETERS:
        return deny(
            f"'{command}' is a shell interpreter and is blocked — its payload "
            "would bypass path containment. Call the real tool directly.",
            code=harness.GUARD_SHELL_DENIED_COMMAND,
        )
    if shutil.which(command) is None:
        return deny(
            f"'{command}' is not an installed executable on this machine.",
            code=harness.GUARD_SHELL_DENIED_COMMAND,
        )

    raw_args = args.get("args") or []
    if not isinstance(raw_args, list) or len(raw_args) > _ARG_CAP:
        return deny(
            f"args must be a list of at most {_ARG_CAP} strings.",
            code=harness.GUARD_SHELL_DENIED_COMMAND,
        )
    str_args = [str(a) for a in raw_args]

    # 1. Approval: read-only inspection auto-runs; anything else needs the
    #    user's explicit ask (scope='shell' card, or the command in their words).
    if not _is_read_only(command, str_args):
        from calliope.agent.harness import policy as shell_policy

        if not shell_policy.user_allows_shell(ctx, command):
            return deny(
                f"Running '{command}' is human-in-the-loop. Ask the user with "
                "ask_user(question=\"Run '{command}' in the agent workspace?\", "
                "options=[\"Yes, run it\", \"No\"], scope=\"shell\") and retry "
                "after they answer — read-only inspection (ffprobe) does not "
                "need this.".replace("{command}", command),
                code=harness.GUARD_SHELL_APPROVAL,
            )

    # 2. cwd containment: the process is born inside the workspace or nowhere.
    ws = settings.workspace_dir
    roots = [ws, settings.assets_dir]
    cwd_arg = args.get("cwd")
    if cwd_arg is not None:
        cwd_path = Path(str(cwd_arg))
        if not _contained(cwd_path, [ws]):
            return deny(
                f"cwd '{cwd_arg}' resolves outside the agent workspace "
                f"({ws}) — the shell cannot leave that folder.",
                code=harness.GUARD_SHELL_OUTSIDE_WORKSPACE,
            )
        cwd = cwd_path.resolve()
    else:
        cwd = ws.resolve()

    # 3. Arg path containment + sensitive-name deny-list.
    for a in str_args:
        if _SENSITIVE_NAME_RE.search(a):
            return deny(
                f"Argument '{a}' names a sensitive file (config/database/"
                "credentials) — the agent shell can never touch those.",
                code=harness.GUARD_SHELL_DENIED_PATH,
            )
        if not _looks_like_path(a):
            continue
        p = Path(a)
        if p.anchor or "/" in a or "\\" in a:
            if not _contained(p, roots):
                return deny(
                    f"Argument '{a}' resolves outside the agent workspace "
                    f"({ws}) and assets folder — pass a path inside them.",
                    code=harness.GUARD_SHELL_OUTSIDE_WORKSPACE,
                )
        elif (p.is_file() or p.is_dir()) and not _contained(p, roots):
            return deny(
                f"Argument '{a}' resolves to an existing path outside the "
                f"agent workspace ({ws}) — pass a path inside it instead.",
                code=harness.GUARD_SHELL_OUTSIDE_WORKSPACE,
            )

    return allow()  # all checks passed


async def t_run_command(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
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


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="run_command",
            description=(
                "Run an installed command-line executable INSIDE the agent "
                "workspace folder (Settings → System Paths). Use for the long "
                "tail the curated tools don't cover: ffprobe inspection of "
                "reference media, batch file checks, quick probes. args is an "
                "argv list — never a shell string; shell interpreters (bash, "
                "powershell, …) are blocked. Read-only inspection runs "
                "directly; anything else needs the user's approval first "
                "(ask them with a scope='shell' ask_user card). Disabled "
                "unless the operator enabled the agent shell in Settings."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Bare executable name, e.g. 'ffprobe' — no paths, no shell",
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Argv list passed verbatim; path arguments must stay inside the agent workspace (assets_dir is allowed for reference inspection)",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Optional working directory; default and only allowed root is the agent workspace",
                    },
                    "timeout_sec": {
                        "type": "number",
                        "description": "Wall-clock cap (default 120s, hard-capped by the operator's tool timeout)",
                    },
                },
                "required": ["command"],
            },
            executor=t_run_command,
            category="system",
            requires_project=False,
        )
    )
    registry.on_pre_execute(_shell_guard)
