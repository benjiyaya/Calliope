"""Permission policy — the user's command IS the permission.

A local tool for local users. When someone types "Regenerate the full
script for this project — turn the story into scenes", that is an ORDER,
not a suggestion to negotiate. The policy's jobs:

1. read INTENT from the user's own words (generation ask, confirmation);
2. keep structured approvals (ask_user cards) working;
3. leave SCOPE to the tools' arguments (ids, all_missing, refs) — prose
   never decides scope.

There is deliberately no message-length cap ("regenerate the full script
for this project, keep the characters and locations consistent" is one
command, not a suspicious wall of text), no global-negation veto ("don't
overthink, render all scenes" is a command), and no politeness filter.
Negation is CLAUSE-scoped: only a "no" inside ~4 words of the cue vetoes
that cue.
"""
from __future__ import annotations

import re
from typing import Any

from calliope.agent.harness.registry import ToolContext

# Affirmation cues: an explicit yes OR a regeneration verb — "regenerate
# the script" both asks and authorizes in one message.
_CONFIRM_RE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok|okay|k|confirm(?:ed)?|proceed|"
    r"go\s+ahead|do\s+it|please\s+do|overwrite|replace|append|regenerate|"
    r"redo|re-?do|start\s+over|restart|delete|wipe|reset|from\s+scratch|"
    r"regen\b|rebuild|rewrite|re-?write|go\s+for\s+it|fine|sounds\s+good|"
    r"that'?s\s+fine)\b",
    re.IGNORECASE,
)
_NEGATE_RE = re.compile(
    r"\b(no|not|don'?t|do\s+not|cancel|stop|never|abort|skip|hold\s+on|wait)\b",
    re.IGNORECASE,
)

# Image/video *generation* intent. `video` matches as a SUBSTRING so model /
# compound names keep signaling intent ("text2video", "fastvideoH3_t2v-API");
# everything else is \b-anchored (`vid` must not match "provide").
# clip/shot/storyboard are RENDER-UNIT nouns in Calliope's vocabulary (clips
# are the render units) — "break the beats into the script + shot clips" is a
# generation order, not a text edit (session 908: the video sub-agent's tools
# stayed hidden and it reported "the generation tool is not exposed").
_RENDER_REQUEST_RE = re.compile(
    r"(video|\b(?:render|image|portrait|sheet|artwork|visual|thumbnail|vid|"
    r"clip|shot|storyboard)\w*|"
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


def _cue_is_negated(before: str, after: str) -> bool:
    """Only a short negation directly adjacent to the cue vetoes it."""
    before_words = before.split()
    if before_words and _NEGATE_RE.search(" ".join(before_words[-4:])):
        return True
    if re.match(r"\s*(no|not|don'?t|never)\b", after, re.IGNORECASE):
        return True
    return False


def is_confirmation(text: str) -> bool:
    """True when any clause of the message commands/confirms an action.

    Clause-scoped negation: "don't change the story, regenerate the script"
    confirms (the veto sits in a different clause than the cue); "no, don't
    replace" does not.
    """
    t = user_prose(text)
    if not t:
        return False
    for clause in re.split(r"[,.;!?]|\bbut\b|\bhowever\b", t, flags=re.IGNORECASE):
        m = _CONFIRM_RE.search(clause)
        if not m:
            continue
        before = clause[: m.start()]
        after = clause[m.end() :]
        if not _cue_is_negated(before, after):
            return True
    return False


def is_render_request(text: str) -> bool:
    """True when the message explicitly asks for image/video generation.

    Clause-scoped negation: "no image need, all 20 scenes use this
    text2video workflow" is a render request. A global veto made "do not
    overthink, make the video" read as a refusal and hid the render tools
    (canvas/62). Only a negation adjacent to the cue counts against it.
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
            if not _cue_is_negated(before, after):
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
