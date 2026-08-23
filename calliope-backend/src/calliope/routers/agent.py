"""Agent session chat API."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from calliope.agent.harness.runner import runner
from calliope.config import settings
from calliope.db import get_db, row_to_dict
from calliope.events.bus import event_bus

router = APIRouter()


class SessionCreate(BaseModel):
    title: str = "New chat"
    project_id: int | None = None

    @field_validator("title")
    @classmethod
    def _bound_title(cls, v: str) -> str:
        if len(v) > 300:
            raise ValueError("Title too long (max 300 characters)")
        return v


class SessionPatch(BaseModel):
    title: str | None = None
    project_id: int | None = None
    unlink: bool = False

    @field_validator("title")
    @classmethod
    def _bound_title(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 300:
            raise ValueError("Title too long (max 300 characters)")
        return v


class MessageMention(BaseModel):
    type: Literal["workflow"] = "workflow"
    id: int
    name: str = Field(default="", max_length=200)
    kind: Literal["image", "video"] = "image"


class MessageAttachment(BaseModel):
    path: str = Field(min_length=1, max_length=2000)
    name: str = Field(default="", max_length=500)
    kind: Literal["image", "video", "audio"] = "image"


class MessageCreate(BaseModel):
    content: str = ""
    mentions: list[MessageMention] = Field(default_factory=list, max_length=1)
    attachments: list[MessageAttachment] = Field(default_factory=list, max_length=8)

    @field_validator("content")
    @classmethod
    def _bound_content(cls, v: str) -> str:
        # Bound the user message: a multi-MB paste would otherwise be stored,
        # echoed to every SSE subscriber, and sent to the LLM verbatim.
        if len(v) > 100_000:  # ~100 KB of text
            raise ValueError("Message too long (max 100,000 characters)")
        return v

    @field_validator("mentions")
    @classmethod
    def _one_workflow(cls, v: list[MessageMention]) -> list[MessageMention]:
        if len(v) > 1:
            raise ValueError("Only one @workflow mention per message")
        return v


def _session_out(conn, row) -> dict[str, Any]:
    session = row_to_dict(row)
    if session.get("project_id"):
        proj = conn.execute(
            "SELECT id, title, status FROM projects WHERE id = ?", (session["project_id"],)
        ).fetchone()
        session["project"] = row_to_dict(proj) if proj else None
    else:
        session["project"] = None
    return session


async def session_event_payload(session_id: int) -> dict[str, Any] | None:
    """Enriched session dict for events: project info + live running flag.
    Used by the harness (link/create_project) and the router so every
    `agent.session.updated` event carries the same shape as GET /sessions."""
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT * FROM agent_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            return None
        session = _session_out(conn, row)
    finally:
        conn.close()
    session["running"] = runner.is_running(session_id)
    return session


async def _publish_session(session_id: int) -> dict[str, Any] | None:
    session = await session_event_payload(session_id)
    if session:
        await event_bus.publish("agent.session.updated", {"session": session})
    return session


def _rows_to_messages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give derived chat rows the agent_messages response shape (id/role/
    content/agent_name/tool_name/tool_args/tool_result/status/created_at)."""
    out: list[dict[str, Any]] = []
    for i, r in enumerate(rows, start=1):
        out.append(
            {
                "id": -i,  # derived rows have no DB id; negative keeps ordering unique
                "session_id": None,
                "role": r["role"],
                "content": r.get("content") or "",
                "agent_name": r.get("agent_name"),
                "tool_name": r.get("tool_name"),
                "tool_args": r.get("tool_args"),
                "tool_result": r.get("tool_result"),
                "status": r.get("status"),
                "reasoning": r.get("reasoning"),
                "mentions": r.get("mentions") or [],
                "attachments": r.get("attachments") or [],
                "created_at": None,
            }
        )
    return out


@router.get("/sessions")
async def list_sessions(project_id: int | None = None) -> list[dict[str, Any]]:
    conn = get_db(settings.db_path)
    try:
        if project_id is not None:
            rows = conn.execute(
                "SELECT * FROM agent_sessions WHERE project_id = ? ORDER BY updated_at DESC",
                (project_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agent_sessions ORDER BY updated_at DESC"
            ).fetchall()
        out = []
        for r in rows:
            s = _session_out(conn, r)
            s["running"] = runner.is_running(s["id"])
            out.append(s)
        return out
    finally:
        conn.close()


@router.post("/sessions")
async def create_session(payload: SessionCreate) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        if payload.project_id is not None:
            if not conn.execute(
                "SELECT id FROM projects WHERE id = ?", (payload.project_id,)
            ).fetchone():
                raise HTTPException(status_code=404, detail="Project not found")
        cur = conn.execute(
            "INSERT INTO agent_sessions (title, project_id) VALUES (?, ?)",
            (payload.title, payload.project_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM agent_sessions WHERE id = ?", (cur.lastrowid,)).fetchone()
        session = _session_out(conn, row)
    finally:
        conn.close()
    session["running"] = False
    await event_bus.publish("agent.session.updated", {"session": session})
    return session


@router.get("/projects")
async def list_projects_for_link() -> list[dict[str, Any]]:
    """Projects available for linking (id/title/status only) — powers the
    sandbox session's link picker."""
    conn = get_db(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT id, title, status FROM projects WHERE status != 'system' ORDER BY updated_at DESC"
        ).fetchall()
        return [row_to_dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/sessions/{session_id}")
async def get_session(session_id: int) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT * FROM agent_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")
        session = _session_out(conn, row)
        session["running"] = runner.is_running(session_id)
    finally:
        conn.close()
    from calliope.agent.harness import log as session_log

    session_log.backfill_from_messages(session_id)
    events = session_log.read_events(session_id)
    session["messages"] = _rows_to_messages(session_log.derive_chat_rows(events))
    session["plan"] = session_log.derive_plan(events)
    return session


@router.patch("/sessions/{session_id}")
async def patch_session(session_id: int, payload: SessionPatch) -> dict[str, Any]:
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT * FROM agent_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")
        if runner.is_running(session_id):
            raise HTTPException(status_code=409, detail="Session is running")
        if payload.title is not None:
            conn.execute(
                "UPDATE agent_sessions SET title = ? WHERE id = ?",
                (payload.title, session_id),
            )
        if payload.unlink:
            conn.execute(
                "UPDATE agent_sessions SET project_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (session_id,),
            )
        elif payload.project_id is not None:
            if not conn.execute(
                "SELECT id FROM projects WHERE id = ?", (payload.project_id,)
            ).fetchone():
                raise HTTPException(status_code=404, detail="Project not found")
            conn.execute(
                "UPDATE agent_sessions SET project_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (payload.project_id, session_id),
            )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM agent_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")
    finally:
        conn.close()
    session = await _publish_session(session_id)
    return session


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: int) -> dict[str, bool]:
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT id FROM agent_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")
        if runner.is_running(session_id):
            raise HTTPException(status_code=409, detail="Session is running")
        conn.execute("DELETE FROM agent_messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM agent_sessions WHERE id = ?", (session_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@router.post("/sessions/{session_id}/messages")
async def post_message(session_id: int, payload: MessageCreate) -> dict[str, Any]:
    content = payload.content.strip()
    if not content and not payload.mentions and not payload.attachments:
        raise HTTPException(status_code=422, detail="Message content is empty")
    from calliope.routers.playground import _assert_path_in_assets

    attachments: list[dict[str, Any]] = []
    for att in payload.attachments:
        resolved = _assert_path_in_assets(att.path)
        attachments.append(
            {
                "path": str(resolved),
                "name": att.name or resolved.name,
                "kind": att.kind,
            }
        )
    mentions = [m.model_dump() for m in payload.mentions]
    try:
        user_msg = await runner.start_turn(
            session_id,
            content,
            mentions=mentions or None,
            attachments=attachments or None,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "message": user_msg}


@router.post("/sessions/{session_id}/cancel")
async def cancel_session(session_id: int) -> dict[str, Any]:
    cancelled = await runner.cancel(session_id)
    return {"ok": cancelled}
