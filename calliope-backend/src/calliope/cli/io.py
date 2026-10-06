"""Input handling: JSON payloads, shared flags, exit codes.

Exit codes are a contract with the opencode skill. The skill branches on them
rather than parsing text, so they must not drift:

    0  success
    2  validation failed (input wrong -- fix the payload, don't re-read)
    3  drift detected (someone else changed it -- re-read, then re-apply)
    4  not found
    5  policy refusal
    1  anything else
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from calliope.authoring.service import (
    AuthoringError,
    DriftDetected,
    NotFound,
    PolicyError,
    ValidationFailed,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_VALIDATION = 2
EXIT_DRIFT = 3
EXIT_NOT_FOUND = 4
EXIT_POLICY = 5


def exit_code_for(exc: BaseException) -> int:
    if isinstance(exc, ValidationFailed):
        return EXIT_VALIDATION
    if isinstance(exc, DriftDetected):
        return EXIT_DRIFT
    if isinstance(exc, NotFound):
        return EXIT_NOT_FOUND
    if isinstance(exc, PolicyError):
        return EXIT_POLICY
    return EXIT_ERROR


def load_payload(path: str | None) -> Any:
    """Read a JSON payload from ``--file``.

    ``-`` means stdin, so an agent can pipe content it already holds in memory
    instead of writing a temp file.
    """
    if path is None:
        return None
    if path == "-":
        raw = sys.stdin.read()
        source = "<stdin>"
    else:
        p = Path(path)
        if not p.exists():
            raise ValidationFailed(
                f"payload file not found: {path}",
                [{"loc": "file", "msg": "no such file"}],
            )
        raw = p.read_text(encoding="utf-8")
        source = path
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationFailed(
            f"{source} is not valid JSON: {exc.msg} (line {exc.lineno}, col {exc.colno})",
            [{"loc": "$", "msg": exc.msg}],
        ) from exc


def read_idea_file(path: str) -> str:
    """Read a long-form source text from ``--idea-file``.

    ``-`` means stdin, same as ``--file``. Newlines are normalised to ``\\n``:
    the text is stored in one TEXT column and read back in JSON, so a stray
    CRLF would show up as a difference between what was written and what comes
    back, forever.
    """
    if path == "-":
        raw, source = sys.stdin.read(), "<stdin>"
    else:
        p = Path(path)
        if not p.exists():
            raise ValidationFailed(
                f"source text file not found: {path}",
                [{"loc": "idea_file", "msg": "no such file"}],
            )
        raw, source = p.read_text(encoding="utf-8"), path
    if not raw.strip():
        raise ValidationFailed(
            f"{source} is empty",
            [{"loc": "idea_file", "msg": "nothing to write"}],
        )
    return raw.replace("\r\n", "\n").replace("\r", "\n")


def problem_report(exc: AuthoringError) -> dict[str, Any]:
    """Machine-readable error shape for ``--json``."""
    out: dict[str, Any] = {"error": type(exc).__name__, "message": str(exc)}
    problems = getattr(exc, "problems", None)
    if problems:
        out["problems"] = problems
    report = getattr(exc, "report", None)
    if report:
        out["report"] = report
    return out


def add_common(parser: argparse.ArgumentParser) -> None:
    """Add ``--json`` / ``--dry-run`` to a parser.

    ``default=SUPPRESS`` is load-bearing. These two are registered on the root
    parser *and* on every group/verb parser, so they may be written either side
    of the command path::

        calliope-cli --json project list
        calliope-cli project list --json

    A subparser's ``store_true`` default is ``False``, and argparse applies it
    while parsing into the shared namespace -- so without SUPPRESS the second
    form worked and the first was silently downgraded to human text. An agent
    that types the natural order gets output it cannot parse, with no error.
    With SUPPRESS an absent flag leaves the attribute alone, and the root's value
    survives. Callers must therefore read these via ``getattr(args, name, False)``
    rather than ``args.name``.
    """
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="machine-readable output",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="validate and report both sides without writing (plan §5.2)",
    )


def resolve_project_arg(conn, value: str) -> int:
    from calliope.authoring.service import resolve_project

    return resolve_project(conn, value)
