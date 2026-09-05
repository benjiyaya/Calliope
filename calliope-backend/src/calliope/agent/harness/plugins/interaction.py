"""Interaction plugin: HITL question cards (ask_user).

The agent ends its turn by asking the user a structured question; the answer
arrives as a `question/answered` event plus a normal user message. Guards read
the structured answer instead of parsing prose (policy.py heuristics remain as
the fallback path).
"""
from __future__ import annotations

import logging
from typing import Any

from calliope.agent.harness import log as session_log
from calliope.agent.harness.registry import ToolContext, ToolDefinition, ToolRegistry

logger = logging.getLogger("calliope.harness.plugins.interaction")

ALLOWED_SCOPES = ("render", "destructive_replace", "info")
_QUESTION_CAP = 500
_OPTION_CAP = 6
_OPTION_LEN_CAP = 120


def latest_open_question(session_id: int) -> dict[str, Any] | None:
    """The most recent question/asked with no later matching answer, if any."""
    from calliope.agent.harness.log import read_events

    events = read_events(session_id)
    open_seq: int | None = None
    question: dict[str, Any] | None = None
    for ev in events:
        if ev.type == session_log.QUESTION_ASKED:
            open_seq = ev.seq
            question = ev.data
        elif ev.type == session_log.QUESTION_ANSWERED and open_seq is not None:
            if ev.data.get("question_seq") == open_seq:
                open_seq = None
                question = None
    if open_seq is None or question is None:
        return None
    return {"seq": open_seq, **question}


async def t_ask_user(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    question = str(args.get("question") or "").strip()
    if not question:
        return {"ok": False, "error": "question is required"}
    if len(question) > _QUESTION_CAP:
        return {"ok": False, "error": f"question must be <= {_QUESTION_CAP} chars"}

    raw_options = args.get("options") or []
    if not isinstance(raw_options, list):
        return {"ok": False, "error": "options must be a list of strings"}
    options: list[str] = []
    for opt in raw_options:
        text = str(opt).strip()
        if text:
            options.append(text[:_OPTION_LEN_CAP])
    options = options[:_OPTION_CAP]
    if len(options) < 2:
        return {"ok": False, "error": "offer at least two options (e.g. yes / no)"}

    scope = str(args.get("scope") or "info").strip()
    if scope not in ALLOWED_SCOPES:
        return {"ok": False, "error": f"scope must be one of {list(ALLOWED_SCOPES)}"}

    data = {"question": question, "options": options, "scope": scope}
    ev = session_log.append_event(ctx.session_id, session_log.QUESTION_ASKED, data)
    return {
        "ok": True,
        "awaiting_user_input": True,
        "question_seq": ev.seq,
        "question": question,
        "options": options,
        "scope": scope,
        "note": (
            "Your turn ENDS here. The user will answer via the question card; "
            "their reply arrives as the next user message. Do not call tools "
            "until they answer."
        ),
    }


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="ask_user",
            description=(
                "Ask the user a structured question with clickable options and END "
                "your turn to wait for the answer. Use this whenever the next step "
                "needs an explicit user choice — especially before expensive or "
                "hard-to-reverse actions (generate images/videos, replace content): "
                'the user\'s click is recorded as an approval, e.g. ask_user('
                'question="Generate images for all 3 characters?", '
                'options=["Yes, generate", "No, not yet"], scope="render"). '
                "scope: 'render' for generation approval, 'destructive_replace' for "
                "content replacement, 'info' for everything else. Returns "
                "awaiting_user_input — treat it as the end of your turn."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to show (<= 500 chars)",
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2-6 short clickable answers, first may be the affirmative one",
                    },
                    "scope": {
                        "type": "string",
                        "enum": list(ALLOWED_SCOPES),
                        "description": "render | destructive_replace | info (default info)",
                    },
                },
                "required": ["question", "options"],
            },
            executor=t_ask_user,
            category="interaction",
            requires_project=False,
        )
    )
