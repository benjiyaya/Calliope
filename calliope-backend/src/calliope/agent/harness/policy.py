"""HITL / confirmation policy — used by guards and tool-payload filtering.

Kept out of ``__init__`` / ``registry`` so those modules can import this
without a cycle. Intent is always derived from the latest user prose, never
from the machine ``[Calliope context]`` appendix.
"""
from __future__ import annotations

import re

from calliope.agent.harness.registry import ToolContext

# Affirmation / destructive-intent cues for an explicit user "yes"
# (or "replace it" / "start over") for the destructive guard.
_CONFIRM_RE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok|okay|k|confirm(?:ed)?|proceed|"
    r"go\s+ahead|do\s+it|please\s+do|overwrite|replace|append|regenerate|"
    r"redo|re-?do|start\s+over|restart|delete|wipe|reset|from\s+scratch|"
    r"go\s+for\s+it|fine|sounds\s+good|that'?s\s+fine)\b",
    re.IGNORECASE,
)
_NEGATE_RE = re.compile(
    r"\b(no|not|don'?t|do\s+not|cancel|stop|never|abort|skip|hold\s+on|wait)\b",
    re.IGNORECASE,
)

# Image/video *generation* intent. A bare "generate" is skipped on purpose
# (it would also match "generate the story").
_RENDER_REQUEST_RE = re.compile(
    r"\b(render\w*|image\w*|video\w*|portrait\w*|sheet\w*|artwork\w*|visual\w*|thumbnail\w*)\b",
    re.IGNORECASE,
)

_APPENDIX_MARK = "[Calliope context]"


def user_prose(text: str) -> str:
    """Visible user words only — drop a trailing Calliope context appendix."""
    raw = text or ""
    if _APPENDIX_MARK in raw:
        raw = raw.split(_APPENDIX_MARK, 1)[0]
    return raw.strip()


def is_confirmation(text: str) -> bool:
    """A terse, non-negated message carrying an explicit affirmative cue."""
    t = user_prose(text)
    if not t or len(t) > 200:
        return False
    if _NEGATE_RE.search(t):
        return False
    return bool(_CONFIRM_RE.search(t))


def is_render_request(text: str) -> bool:
    """True when the message explicitly asks for image/video generation."""
    t = user_prose(text)
    if not t or _NEGATE_RE.search(t):
        return False
    return bool(_RENDER_REQUEST_RE.search(t))


# More than this many clips needs an explicit "all scenes / every clip" in
# the latest user prose. A bare "yes" after a 2-clip offer must not enqueue
# the whole timeline (the model has dumped every scene_id before).
BULK_ENQUEUE_LIMIT = 3
_BULK_VIDEO_RE = re.compile(
    r"\b("
    r"all(\s+the)?\s+(scenes?|clips?|videos?|shots?)"
    r"|every\s+(scene|clip|shot)"
    r"|entire\s+(film|project|script|timeline)"
    r"|whole\s+(film|project|script)"
    r"|all\s+remaining"
    r")\b",
    re.IGNORECASE,
)


def allows_bulk_video_enqueue(text: str, count: int) -> bool:
    """True when `count` clips is a small targeted batch, or the user asked for all."""
    if count <= BULK_ENQUEUE_LIMIT:
        return True
    return bool(_BULK_VIDEO_RE.search(user_prose(text)))


def user_allows_render(ctx: ToolContext) -> bool:
    """Latest user prose asks to generate, or tersely confirms an offer."""
    from calliope.agent.harness import log as session_log

    latest = session_log.latest_user_message(ctx.session_id) or ""
    return is_render_request(latest) or is_confirmation(latest)
