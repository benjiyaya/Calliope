"""Append-only session events. Chat rows and the plan are derived from them."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from calliope.config import settings
from calliope.db import get_db, row_to_dict

MEMORY_SAVED = "memory/saved"
# HITL question cards: the agent asks (the runner stamps the real event seq
# onto the ask_user tool result), the user answers by clicking an option —
# the answer arrives as a user/message with answer_to == question seq plus a
# question/answered marker. ask_user with scope render/destructive_replace/
# shell is what guards check via has_structured_approval.
QUESTION_ASKED = "question/asked"
QUESTION_ANSWERED = "question/answered"

_CHAT_TYPES = {
    "user/message": "user",
    "assistant/message": "assistant",
    "tool/result": "tool",
    "steering/message": "user",
}


@dataclass
class Event:
    seq: int
    type: str
    data: dict[str, Any]


def _load(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def append_event(session_id: int, event_type: str, data: dict[str, Any] | None = None) -> int:
    payload = data or {}
    conn = get_db(settings.db_path)
    try:
        current = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM agent_events WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        seq = int(current[0]) + 1
        conn.execute(
            """
            INSERT INTO agent_events (session_id, seq, type, data_json)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, seq, event_type, json.dumps(payload)),
        )
        conn.commit()
        return seq
    finally:
        conn.close()


def read_events(session_id: int) -> list[Event]:
    conn = get_db(settings.db_path)
    try:
        rows = conn.execute(
            """
            SELECT seq, type, data_json FROM agent_events
            WHERE session_id = ? ORDER BY seq
            """,
            (session_id,),
        ).fetchall()
        return [
            Event(seq=int(row["seq"]), type=row["type"], data=_load(row["data_json"]))
            for row in rows
        ]
    finally:
        conn.close()


def backfill_from_messages(session_id: int) -> None:
    """First read of a legacy session copies agent_messages into the log."""
    conn = get_db(settings.db_path)
    try:
        existing = conn.execute(
            "SELECT 1 FROM agent_events WHERE session_id = ? LIMIT 1",
            (session_id,),
        ).fetchone()
        if existing:
            return
        rows = conn.execute(
            """
            SELECT * FROM agent_messages
            WHERE session_id = ? ORDER BY id
            """,
            (session_id,),
        ).fetchall()
        if not rows:
            return
        for seq, row in enumerate(rows, start=1):
            item = row_to_dict(row)
            role = item.get("role") or "user"
            event_type = {
                "user": "user/message",
                "assistant": "assistant/message",
                "tool": "tool/result",
                "system": "assistant/message",
            }.get(role, "user/message")
            data = {
                "role": role,
                "content": item.get("content") or "",
                "agent_name": item.get("agent_name"),
                "tool_name": item.get("tool_name"),
                "status": item.get("status"),
            }
            if item.get("tool_args_json"):
                data["tool_args"] = _load(item["tool_args_json"])
            if item.get("tool_result_json"):
                try:
                    data["tool_result"] = json.loads(item["tool_result_json"])
                except json.JSONDecodeError:
                    data["tool_result"] = item["tool_result_json"]
            conn.execute(
                """
                INSERT INTO agent_events (session_id, seq, type, data_json)
                VALUES (?, ?, ?, ?)
                """,
                (session_id, seq, event_type, json.dumps(data)),
            )
        conn.commit()
    finally:
        conn.close()


def derive_chat_rows(events: list[Event]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        role = _CHAT_TYPES.get(event.type)
        if role is None:
            continue
        data = event.data
        rows.append(
            {
                "role": data.get("role") or role,
                "content": data.get("content") or "",
                "agent_name": data.get("agent_name"),
                "tool_name": data.get("tool_name"),
                "tool_args": data.get("tool_args"),
                "tool_result": data.get("tool_result"),
                "status": data.get("status")
                or ("steering" if event.type == "steering/message" else None),
                "reasoning": data.get("reasoning"),
                "mentions": data.get("mentions") or [],
                "attachments": data.get("attachments") or [],
            }
        )
    return rows


def derive_plan(events: list[Event]) -> dict[str, Any] | None:
    plan: dict[str, Any] | None = None
    for event in events:
        if event.type == "plan/created":
            tasks = event.data.get("tasks") or []
            plan = {
                "tasks": [dict(task) for task in tasks if isinstance(task, dict)],
                "note": event.data.get("note"),
            }
        elif event.type in ("task/start", "task/end") and plan is not None:
            index = event.data.get("index")
            tasks = plan["tasks"]
            if isinstance(index, int) and 0 <= index < len(tasks) and event.data.get("status"):
                tasks[index]["status"] = event.data["status"]
    return plan


def _image_attachment_data_url(path: str) -> str | None:
    """Data URL for a reference image under assets_dir. Missing or huge files return None."""
    if not path:
        return None
    target = Path(path)
    if not target.is_file():
        return None
    try:
        target.resolve().relative_to(settings.assets_dir.resolve())
    except ValueError:
        return None
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(target.suffix.lower())
    if mime is None:
        return None
    raw = target.read_bytes()
    if len(raw) > 512_000:
        return None
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def steering_after(session_id: int, seq: int) -> tuple[list[str], int]:
    """Steering notes with seq greater than the watermark, plus the new watermark."""
    notes: list[str] = []
    latest = seq
    for event in read_events(session_id):
        if event.seq <= seq:
            continue
        latest = event.seq
        if event.type == "steering/message":
            text = (event.data.get("content") or "").strip()
            if text:
                notes.append(text)
    return notes, latest


_REFUSAL_RE = re.compile(r"\b(no|cancel|stop|don'?t|deny|reject|abort|skip)\b", re.I)


def has_structured_approval(session_id: int, scope: str) -> bool:
    """True when the newest ask_user card of `scope` was answered affirmatively.

    The card comes from a question/asked event; the answer is a
    question/answered marker whose question_seq matches, written by the
    message endpoint when the user clicks an option (the accompanying
    user/message carries answer_to). A refusal option ("No") does NOT count —
    the answer text is checked against a refusal pattern. Each new card of
    the scope resets the verdict, so only the newest card's answer stands.
    """
    approved = False
    latest_question_seq: int | None = None
    for event in read_events(session_id):
        if event.type == QUESTION_ASKED:
            if str(event.data.get("scope") or "") == scope:
                latest_question_seq = event.seq
                approved = False  # a new card voids the previous verdict
        elif event.type == QUESTION_ANSWERED and latest_question_seq is not None:
            try:
                answered = int(event.data.get("question_seq") or 0)
            except (TypeError, ValueError):
                answered = 0
            if answered == latest_question_seq:
                answer = str(event.data.get("answer") or "")
                approved = not _REFUSAL_RE.search(answer)
    return approved
