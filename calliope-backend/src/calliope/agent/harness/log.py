"""Append-only session event log — the single source of truth.

Ported from the deepseek-harness session store: an agent session is an
append-only log of typed `SessionEvent`s. LLM history is *derived* from the
log (`derive_llm_history`), never stored separately; chat rows for the UI are
derived too (`derive_chat_rows` — one row per user/assistant/tool fact, the
same shape agent_messages rows had).

Events are persisted in `agent_events` (per session, ordered by seq). Legacy
sessions that only have `agent_messages` are backfilled on first read, so old
chats keep working.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from calliope.config import settings
from calliope.db import get_db, row_to_dict


# Event types (a closed vocabulary; new types may be added, readers ignore
# unknown ones — same growth rule as deepseek-harness SessionEventMap).
TURN_START = "turn/start"
TURN_END = "turn/end"
STEP_START = "step/start"
STEP_END = "step/end"
USER_MESSAGE = "user/message"
ASSISTANT_MESSAGE = "assistant/message"
TOOL_CALL = "tool/call"
TOOL_RESULT = "tool/result"
# Swarm planner events: the plan (task list) + per-task lifecycle markers.
PLAN_CREATED = "plan/created"
TASK_START = "task/start"
TASK_END = "task/end"

TOOL_RESULT_TRUNCATE = 4000
# Appended wherever a tool result is cut for the LLM. Must TEACH the way out —
# a bare "[truncated]" sends agents into retry loops hoping for a different
# cut (observed live 2026-08-24: an assets sub-agent re-called get_workspace
# repeatedly, blind to characters/locations below the cut).
TRUNCATE_NOTE = (
    "…[truncated — the full result is too large for one reply. Fetch a "
    "smaller slice instead of retrying: get_workspace accepts "
    "sections=[\"characters\",\"locations\",\"items\",\"beats\",\"scenes\"], "
    "and scoped tools (get_story, list_scenes) return less.]"
)

# Cap for ANY single event payload at append time: keeps the append-only log
# faithful in shape but bounded in size (a 500 KB tool result would otherwise
# be re-sent to the LLM / re-read by the UI on every turn).
EVENT_DATA_TRUNCATE = 16_000


def _bounded_data(data: dict[str, Any]) -> dict[str, Any]:
    """Cap oversized payload values (tool results mainly) in place-ish."""
    out: dict[str, Any] = {}
    for k, v in data.items():
        if k == "result" and v is not None:
            text = json.dumps(v, ensure_ascii=False, default=str)
            if len(text) > EVENT_DATA_TRUNCATE:
                out[k] = {
                    "truncated": True,
                    "preview": text[:EVENT_DATA_TRUNCATE] + "…[truncated]",
                }
                continue
        out[k] = v
    return out


@dataclass
class SessionEvent:
    seq: int
    type: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "type": self.type, "data": self.data}


def _db() -> sqlite3.Connection:
    return get_db(settings.db_path)


# ─────────────────────────────────────────────────────────────────────────
# Append / read
# ─────────────────────────────────────────────────────────────────────────


def append_event(session_id: int, event_type: str, data: dict[str, Any]) -> SessionEvent:
    # Atomic single-statement seq allocation: concurrent appenders can never
    # observe the same MAX(seq) (the read-and-write is one INSERT..SELECT
    # under SQLite's write lock) and cannot collide on UNIQUE(session_id, seq).
    data = _bounded_data(data)
    conn = _db()
    try:
        cur = conn.execute(
            """
            INSERT INTO agent_events (session_id, seq, type, data_json)
            VALUES (?, COALESCE((SELECT MAX(seq) FROM agent_events WHERE session_id = ?), 0) + 1, ?, ?)
            """,
            (session_id, session_id, event_type, json.dumps(data, ensure_ascii=False, default=str)),
        )
        seq = cur.lastrowid
        conn.execute(
            "UPDATE agent_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (session_id,),
        )
        conn.commit()
        return SessionEvent(seq=seq, type=event_type, data=data)
    finally:
        conn.close()


def read_events(session_id: int) -> list[SessionEvent]:
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM agent_events WHERE session_id = ? ORDER BY seq", (session_id,)
        ).fetchall()
        events: list[SessionEvent] = []
        for r in rows:
            try:
                data = json.loads(r["data_json"])
            except (json.JSONDecodeError, TypeError):
                data = {}
            events.append(SessionEvent(seq=r["seq"], type=r["type"], data=data))
        return events
    finally:
        conn.close()


def latest_user_message(session_id: int) -> str | None:
    """Most recent `user/message` content for a session (or None).

    Used by policy hooks (e.g. the destructive-action guard) to decide whether
    the user explicitly confirmed an action in their latest message.

    Returns the user's visible prose only — never the machine `[Calliope
    context]` appendix (which can contain `kind=image` and would otherwise
    auto-approve HITL renders).
    """
    conn = _db()
    try:
        row = conn.execute(
            "SELECT data_json FROM agent_events "
            "WHERE session_id = ? AND type = ? ORDER BY seq DESC LIMIT 1",
            (session_id, USER_MESSAGE),
        ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["data_json"])
        except (json.JSONDecodeError, TypeError):
            return None
        content = data.get("content")
        if not isinstance(content, str):
            return None
        # Never treat the machine appendix as user intent (kind=image, etc.).
        prose = content.split("[Calliope context]", 1)[0].strip()
        return prose or None
    finally:
        conn.close()


def format_calliope_context(
    mentions: list[dict[str, Any]] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> str:
    """Machine appendix projected onto LLM user content (ids, not just names)."""
    wf_lines: list[str] = []
    att_lines: list[str] = []
    for m in mentions or []:
        if not isinstance(m, dict):
            continue
        if m.get("type") not in (None, "workflow"):
            continue
        try:
            wid = int(m["id"])
        except (KeyError, TypeError, ValueError):
            continue
        name = str(m.get("name") or "").replace('"', "'")
        kind = str(m.get("kind") or "image")
        wf_lines.append(f'workflow_id={wid} name="{name}" kind={kind}')
    for a in attachments or []:
        if not isinstance(a, dict):
            continue
        path = str(a.get("path") or "").strip()
        if not path:
            continue
        kind = str(a.get("kind") or "image")
        att_lines.append(f"attached: {path} ({kind})")
    # Guardrail: one Calliope workflow per turn so the model cannot fan out
    # run_workflow across several tagged ids.
    lines = ([wf_lines[0]] if wf_lines else []) + att_lines
    if not lines:
        return ""
    return "[Calliope context]\n" + "\n".join(lines)


def project_user_content(
    content: str,
    mentions: list[dict[str, Any]] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> str:
    """User prose plus the Calliope context appendix for the LLM."""
    appendix = format_calliope_context(mentions, attachments)
    prose = (content or "").rstrip()
    if not appendix:
        return prose
    if not prose:
        return appendix
    return f"{prose}\n\n{appendix}"


def max_turn_number(session_id: int) -> int:
    """Highest recorded turn number for the session (0 when none).

    Cheaper than read_events for turn numbering: reads only turn/start rows,
    not every event payload in the session.
    """
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT data_json FROM agent_events WHERE session_id = ? AND type = ?",
            (session_id, TURN_START),
        ).fetchall()
        best = 0
        for r in rows:
            try:
                turn = int(json.loads(r["data_json"]).get("turn", 0))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            best = max(best, turn)
        return best
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────
# Derivations
# ─────────────────────────────────────────────────────────────────────────


def derive_llm_history(
    events: list[SessionEvent], max_user_turns: int | None = None
) -> list[dict[str, Any]]:
    """Project OpenAI-format LLM history from the event log.

    Rules:
    - user/message   → user role
    - assistant/message → assistant role (content + tool_calls)
    - tool/result    → tool role with tool_call_id (attached to the step's
                       tool/call events)
    - Turn/step boundaries and chunks are replay-only; they do not project.
    - agent_name is flattened into the content for sub-agent turns ([name] prefix)

    `max_user_turns` bounds the projection to the last N user messages (and
    everything after each, so tool-call/result pairs always stay complete —
    exchanges never span user turns). None = unbounded (legacy behavior).
    """
    history: list[dict[str, Any]] = []
    user_turn_boundaries: list[int] = []
    tool_call_by_id: dict[str, dict[str, Any]] = {}
    for e in events:
        d = e.data
        if e.type == USER_MESSAGE:
            user_turn_boundaries.append(len(history))
            history.append(
                {
                    "role": "user",
                    "content": project_user_content(
                        d.get("content") or "",
                        d.get("mentions"),
                        d.get("attachments"),
                    ),
                }
            )
        elif e.type == ASSISTANT_MESSAGE:
            msg: dict[str, Any] = {"role": "assistant"}
            name = d.get("agent_name")
            content = d.get("content") or ""
            if name:
                msg["content"] = f"[{name}] {content}" if content else f"[{name}]"
            else:
                msg["content"] = content
            tool_calls = d.get("tool_calls") or []
            if tool_calls:
                msg["tool_calls"] = tool_calls
            history.append(msg)
        elif e.type == TOOL_CALL:
            tool_call_by_id[d.get("call_id", "")] = d
        elif e.type == TOOL_RESULT:
            call = tool_call_by_id.get(d.get("call_id", ""))
            tool_name = d.get("tool_name") or (call or {}).get("tool_name") or "tool"
            result = d.get("result") or {}
            digest = _truncate_result(result)
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": d.get("call_id", ""),
                    "content": f"[{tool_name}] {digest}",
                }
            )
    if max_user_turns is not None and user_turn_boundaries:
        # Keep the last N user turns (and everything after each boundary —
        # tool exchanges never span user turns, so pairs stay intact).
        # Clamp N to the actual turn count: `[-N]` on a shorter list raises
        # IndexError, which a fresh 1-turn session hit (N=40 >> 1 turn).
        if max_user_turns <= 0:
            start = len(history)
        else:
            start = user_turn_boundaries[-min(max_user_turns, len(user_turn_boundaries))]
        if start > 0:
            history = history[start:]
    return history


def _truncate_result(result: Any) -> str:
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > TOOL_RESULT_TRUNCATE:
        return text[:TOOL_RESULT_TRUNCATE] + TRUNCATE_NOTE
    return text


def derive_chat_rows(events: list[SessionEvent]) -> list[dict[str, Any]]:
    """Project UI chat rows (the legacy agent_messages shape) from the log.

    Each tool/call → row without result; the paired tool/result attaches the
    result to that row (matched by call_id)."""
    rows: list[dict[str, Any]] = []
    row_by_call_id: dict[str, dict[str, Any]] = {}
    for e in events:
        d = e.data
        if e.type == USER_MESSAGE:
            row = {"role": "user", "content": d.get("content", "")}
            if d.get("mentions"):
                row["mentions"] = d["mentions"]
            if d.get("attachments"):
                row["attachments"] = d["attachments"]
            rows.append(row)
        elif e.type == TOOL_CALL:
            fn_name = d.get("tool_name") or "tool"
            row: dict[str, Any] = {
                "role": "tool",
                "agent_name": d.get("agent_name"),
                "tool_name": fn_name,
                "tool_args": _parse_args(d.get("arguments")),
                "tool_result": None,
                "content": "",
            }
            rows.append(row)
            call_id = d.get("call_id")
            if call_id:
                row_by_call_id[call_id] = row
        elif e.type == ASSISTANT_MESSAGE:
            name = d.get("agent_name")
            content = (d.get("content") or "").strip()
            if content:
                row: dict[str, Any] = {
                    "role": "assistant",
                    "agent_name": name,
                    "content": content,
                    "status": d.get("status"),
                }
                reasoning = d.get("reasoning")
                if reasoning:
                    row["reasoning"] = reasoning
                rows.append(row)
        elif e.type == TOOL_RESULT:
            row = row_by_call_id.get(d.get("call_id", ""))
            if row is not None:
                row["tool_result"] = d.get("result")
    return rows


def _parse_args(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def derive_plan(events: list[SessionEvent]) -> dict[str, Any] | None:
    """Project the latest swarm plan + per-task status from the event log.

    The most recent `plan/created` event resets the task list (each turn may
    produce a new plan); `task/start` / `task/end` then annotate tasks by index.
    Returns None when no plan exists (simple/legacy turns).
    """
    tasks: list[dict[str, Any]] = []
    note = ""
    statuses: dict[int, str] = {}
    for e in events:
        d = e.data
        if e.type == PLAN_CREATED:
            tasks = []
            statuses = {}
            note = d.get("note") or ""
            for t in d.get("tasks") or []:
                if not isinstance(t, dict):
                    continue
                tasks.append(
                    {
                        "role": t.get("role") or "script",
                        "goal": (t.get("goal") or "").strip(),
                        "status": "pending",
                    }
                )
        elif e.type == TASK_START:
            idx = d.get("index")
            if isinstance(idx, int) and 0 <= idx < len(tasks):
                statuses[idx] = "running"
        elif e.type == TASK_END:
            idx = d.get("index")
            if isinstance(idx, int) and 0 <= idx < len(tasks):
                statuses[idx] = d.get("status") or "done"
    if not tasks:
        return None
    for i, t in enumerate(tasks):
        if i in statuses:
            t["status"] = statuses[i]
    return {"tasks": tasks, "note": note}


# ─────────────────────────────────────────────────────────────────────────
# Legacy backfill: agent_messages → events
# ─────────────────────────────────────────────────────────────────────────


def backfill_from_messages(session_id: int) -> bool:
    """Import a legacy agent_messages trail into the event log (once).

    Returns True when events were appended (first import), False when the
    session already had events (or had no messages).
    """
    conn = _db()
    try:
        has_events = conn.execute(
            "SELECT 1 FROM agent_events WHERE session_id = ? LIMIT 1", (session_id,)
        ).fetchone()
        if has_events:
            return False
        rows = conn.execute(
            "SELECT * FROM agent_messages WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()
        if not rows:
            return False
        events: list[tuple[str, dict[str, Any]]] = []
        for r in rows:
            m = row_to_dict(r)
            role = m["role"]
            if role == "user":
                events.append((USER_MESSAGE, {"content": m["content"]}))
            elif role == "assistant":
                events.append(
                    (
                        ASSISTANT_MESSAGE,
                        {
                            "content": m["content"],
                            "agent_name": m.get("agent_name"),
                            "status": m.get("status"),
                        },
                    )
                )
            elif role == "tool":
                events.append(
                    (
                        TOOL_CALL,
                        {
                            "call_id": f"legacy_{m['id']}",
                            "tool_name": m.get("tool_name"),
                            "arguments": m.get("tool_args_json"),
                        },
                    )
                )
                result = None
                if m.get("tool_result_json"):
                    try:
                        result = json.loads(m["tool_result_json"])
                    except json.JSONDecodeError:
                        result = {"raw": m["tool_result_json"]}
                events.append(
                    (
                        TOOL_RESULT,
                        {
                            "call_id": f"legacy_{m['id']}",
                            "tool_name": m.get("tool_name"),
                            "result": result,
                        },
                    )
                )
        cur_count = 0
        for event_type, data in events:
            cur_count += 1
            conn.execute(
                "INSERT INTO agent_events (session_id, seq, type, data_json) VALUES (?, ?, ?, ?)",
                (session_id, cur_count, event_type, json.dumps(data, ensure_ascii=False, default=str)),
            )
        conn.execute(
            "UPDATE agent_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (session_id,),
        )
        conn.commit()
        return cur_count > 0
    finally:
        conn.close()
