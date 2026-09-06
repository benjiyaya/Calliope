"""HITL / confirmation policy — mechanical gates only.

Design rule (learned the hard way on canvas/62 + canvas/65): policy decides
PERMISSION from explicit user acts; it never guesses SCOPE from prose.
Scope is enforced mechanically by the enqueue tools (explicit ids, or an
explicit all_missing=true / "all scenes" word). Prose heuristics here are
limited to: does this message ask for generation at all, or confirm an
offer — nothing more.

Permission sources (any one grants):
- an ask_user card answered affirmatively with scope=render (structured),
- an @workflow tag (a deliberate picker act),
- render verbs in the user's own words, or a terse confirmation.
"""
from __future__ import annotations

import re
from typing import Any

from calliope.agent.harness.registry import ToolContext

# Affirmation cues for an explicit user "yes" (or "replace it" / "start over").
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

# Image/video *generation* intent. `video` matches as a SUBSTRING so model /
# compound names keep signaling intent ("text2video", "fastvideoH3_t2v-API");
# everything else is \b-anchored (`vid` must not match "provide").
_RENDER_REQUEST_RE = re.compile(
    r"(video|\b(?:render|image|portrait|sheet|artwork|visual|thumbnail|vid)\w*|"
    r"txt2\w+|text2\w+|img2\w+|photo2\w+)",
    re.IGNORECASE,
)

# Compound splitter: "text2video" → "text video" so the bare token matches.
_COMPOUND_SPLIT_RE = re.compile(r"(?<=[a-z])(?:2|to|-)(?=[a-z])", re.IGNORECASE)

_APPENDIX_MARK = "[Calliope context]"


def user_prose(text: str) -> str:
    """Visible user words only — drop the machine `[Calliope context]`
    appendix (it can carry kind=image and would otherwise auto-approve)."""
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
    """True when the message explicitly asks for image/video generation.

    Negation is CLAUSE-scoped, not message-global: "no image need, all 20
    scenes use this text2video workflow" is a render request with a negated
    clause about images. A global veto made "do not overthink, make the
    video" read as a refusal and hid the render tools (the agent then
    fabricated job ids — the bug this keeps fixed). Only a negation in the
    SAME clause as the render cue counts against it.
    """
    t = user_prose(text)
    if not t:
        return False
    expanded = _COMPOUND_SPLIT_RE.sub(" ", t)
    if not _RENDER_REQUEST_RE.search(expanded):
        return False
    for clause in re.split(r"[,.;!?]|\bbut\b|\bhowever\b", expanded, flags=re.IGNORECASE):
        for match in _RENDER_REQUEST_RE.finditer(clause):
            before = clause[max(0, match.start() - 60) : match.start()]
            after = clause[match.end() : match.end() + 30]
            if _NEGATE_RE.search(before) and len(before.split()) <= 4:
                continue
            if re.match(r"\s*(no|not|don'?t|never)\b", after, re.IGNORECASE):
                continue
            return True
    return False


# More than this many targets needs an explicit "all / every / remaining"
# word in the user's prose, or explicit ids in the tool args. A bare "yes"
# after a 2-item offer must not enqueue the whole project (the model has
# dumped every id before).
BULK_ENQUEUE_LIMIT = 3
_BULK_RE = re.compile(
    r"\b("
    r"all(\s+the)?\s+(scenes?|clips?|videos?|shots?|assets?|images?|characters?|locations?|items?)"
    r"|every\s+(scene|clip|shot|asset|image|character|location|item)"
    r"|entire\s+(film|project|script|timeline|cast)"
    r"|whole\s+(film|project|script|cast)"
    r"|all\s+remaining|remaining\s+all"
    r"|rest\s+of\s+the"
    r")\b",
    re.IGNORECASE,
)


def allows_bulk_enqueue(text: str, count: int) -> bool:
    """True when `count` targets is a small batch, or the user asked for all."""
    if count <= BULK_ENQUEUE_LIMIT:
        return True
    return bool(_BULK_RE.search(user_prose(text)))


# Back-compat alias used by the video path.
def allows_bulk_video_enqueue(text: str, count: int) -> bool:
    return allows_bulk_enqueue(text, count)


def user_allows_render(ctx: ToolContext) -> bool:
    """Render permission from explicit user acts only (see module docstring)."""
    if has_structured_approval(ctx, "render"):
        return True
    from calliope.agent.harness import log as session_log

    if session_log.latest_user_has_workflow_tag(ctx.session_id):
        return True
    latest = session_log.latest_user_message(ctx.session_id) or ""
    return is_render_request(latest) or is_confirmation(latest)


# ── Structured approvals (ask_user question cards) ────────────────────────
# An approval is an event-log fact, not parsed prose: question/asked followed
# by question/answered with an affirmative option. The card click sends the
# option text as a normal message with answer_to; the runner records
# question/answered immediately before its user/message echo.

_AFFIRMATIVE_ANSWERS = frozenset({"yes", "y", "ok", "okay", "sure", "confirm", "go ahead", "do it"})
# Card options are full sentences ("Yes, replace with ~40 shorter scenes").
# The FIRST word decides: yes-family grants, no-family refuses, anything
# else falls back to the exact-match set. Mechanical, not prose guessing.
_AFFIRMATIVE_FIRST_WORDS = frozenset(
    {"yes", "yep", "yeah", "yup", "y", "ok", "okay", "sure", "confirm", "confirmed",
     "proceed", "go", "do", "please", "fine", "absolutely", "definitely"}
)
_NEGATIVE_FIRST_WORDS = frozenset(
    {"no", "nope", "not", "dont", "don't", "do not", "never", "cancel", "stop",
     "skip", "wait", "hold"}
)


def _answer_is_affirmative(text: str) -> bool:
    t = text.strip().lower()
    if not t:
        return False
    if t in _AFFIRMATIVE_ANSWERS:
        return True
    first = re.split(r"[,\s.!]+", t, maxsplit=1)[0]
    if first in _AFFIRMATIVE_FIRST_WORDS:
        return True
    if first in _NEGATIVE_FIRST_WORDS or t.startswith(("don't", "do not", "no ")):
        return False
    return False


def latest_answer(session_id: int) -> dict[str, Any] | None:
    """The most recent question/answered event data, or None.

    The answer event and its paired user message arrive together (a card
    click IS a user message). Scanning backwards: a user/message whose
    immediately preceding event is question/answered IS that answer; any
    other user/message is fresh prose and invalidates earlier approvals.
    """
    from calliope.agent.harness import log as session_log
    from calliope.agent.harness.log import read_events

    events = read_events(session_id)
    for i in range(len(events) - 1, -1, -1):
        ev = events[i]
        if ev.type == session_log.QUESTION_ANSWERED:
            return ev.data
        if ev.type == session_log.USER_MESSAGE:
            if i > 0 and events[i - 1].type == session_log.QUESTION_ANSWERED:
                return events[i - 1].data
            return None
    return None


def has_structured_approval(ctx: ToolContext, scope: str) -> bool:
    """True when the latest user-originated input answered a question card
    with the given scope affirmatively. `scope: info` never grants."""
    if scope == "info":
        return False
    answer = latest_answer(ctx.session_id)
    if not answer or answer.get("scope") != scope:
        return False
    return _answer_is_affirmative(str(answer.get("answer") or ""))
