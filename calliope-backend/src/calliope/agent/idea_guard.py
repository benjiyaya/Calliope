"""Long source text. Generators refuse it; project reads only report the flag."""

from __future__ import annotations

from typing import Any

LONG_IDEA_CHARS = 2000


class LongSourceText(ValueError):  # noqa: N818 — routers already catch this name
    """The project idea is source text, not a logline the built-in generators may rewrite."""


def is_long(idea: Any) -> bool:
    return isinstance(idea, str) and len(idea) > LONG_IDEA_CHARS


def refuse_long_idea(project: dict[str, Any] | None, *, generator: str) -> None:
    idea = project.get("idea") if isinstance(project, dict) else None
    if is_long(idea):
        # "This is not allowed" is a dead end. The whole reason for the guard
        # is that there IS a supported path, so the refusal must name it.
        raise LongSourceText(
            f"Project {project.get('id')} stores {len(idea or ''):,} characters in Story "
            f"idea — that is a source text, not a logline. The built-in "
            f"{generator} interpolates it into a single prompt, so it would spend "
            f"a real model call on material the model cannot use and return "
            f"something that silently ignores the novel. Author this project with "
            f"calliope-cli instead (see the `calliope` skill), or shorten Story "
            f"idea to under {LONG_IDEA_CHARS} characters."
        )


def skip_reason(board: dict[str, Any] | None) -> str | None:
    """Reason to skip an LLM pass over a long idea, or None when the idea is short."""
    if not isinstance(board, dict):
        return None
    project = board.get("project")
    if isinstance(project, dict):
        idea = project.get("idea")
    else:
        idea = board.get("idea")
    if is_long(idea):
        return f"idea is source text longer than {LONG_IDEA_CHARS} characters"
    return None
