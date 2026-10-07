"""The long-``idea`` guard: the built-in generators refuse a source text.

``projects.idea`` is two things at once. It is the logline the built-in
generators were written against, and -- since the CLI authoring bridge -- the
repository a whole novel lives in, because that is the textarea the user
already pastes into. Both are the same column, so the built-in generators can
no longer assume they are holding a logline.

They would not notice either way. Each one interpolates ``idea`` into a prompt
without truncation, so a 34,000-character novel costs a real LLM call whose
answer is based on material the model cannot use, and the failure is silent:
the caller gets plausible-looking beats or scenes that have nothing to do with
the novel. Truncating instead of refusing would hide that; refusing is the
only version that is honest about it.

The threshold is the repository's own existing number -- ``harness/plugins/
workspace.py`` already warns about ``project_idea_large`` at 2,000 -- not an
invented one. Anything above it is a source text, not a pitch.

Two different responses, on purpose:

* :func:`refuse_long_idea` raises, for the entry points a user clicks. There
  the right answer is "not this way, use the CLI", and there is no cheap
  fallback worth taking.
* :func:`skip_reason` returns a reason string instead of raising, for the
  continuity planner. That one runs inside the render loop and promises never
  to raise, so refusing there would break rendering for precisely the projects
  this guard is meant to serve. It already has a non-LLM fallback; this just
  picks it on purpose rather than by accident.
"""
from __future__ import annotations

from typing import Any

# Above this many characters, `idea` is a source text rather than a logline.
# Matches `agent/harness/plugins/workspace.py`'s existing `project_idea_large`
# warning, so the repository has one number rather than two that disagree.
LONG_IDEA_CHARS = 2000


class LongSourceText(ValueError):
    """A built-in generator was handed a novel instead of a logline.

    Subclasses ``ValueError`` because that is what the routers already map to
    422, so ``routers/scenes.py`` needs no new branch to surface it.
    """


def is_long(idea: Any) -> bool:
    """True when ``idea`` is a source text rather than a pitch.

    Total on purpose: ``None`` and non-strings are never long, so a caller
    that forgot to coerce cannot accidentally trip the guard.
    """
    return isinstance(idea, str) and len(idea) > LONG_IDEA_CHARS


def refuse_long_idea(project: dict[str, Any], *, generator: str) -> None:
    """Raise unless ``project``'s idea is short enough to prompt with.

    Call this before building any messages, so a refusal costs no tokens. The
    message names the alternative, because "this is not allowed" is a dead end
    and "here is the way that works" is the whole point.
    """
    idea = project.get("idea") or ""
    if not is_long(idea):
        return
    raise LongSourceText(
        f"Project {project.get('id')} stores {len(idea):,} characters in Story "
        f"idea -- that is a source text, not a logline. The built-in "
        f"{generator} interpolates it into a single prompt, so it would spend "
        f"a real model call on material the model cannot use and return "
        f"something that silently ignores the novel. Author this project with "
        f"calliope-cli instead (see the `calliope` skill), or shorten Story "
        f"idea to a pitch."
    )


def skip_reason(board: dict[str, Any]) -> str | None:
    """Why the continuity planner must not call the model, or ``None``.

    Returned rather than raised because the planner runs inside the render
    loop and must keep working on a long-idea project -- the render is the
    user's own next step, and breaking it would make the guard worse than the
    burn it prevents.
    """
    idea = (board.get("project") or {}).get("idea") or ""
    if not is_long(idea):
        return None
    return (
        f"project idea is a {len(idea):,}-character source text; using the "
        f"board plan instead of calling the model"
    )
