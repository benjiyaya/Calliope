"""Agent session chat API."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator

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
    type: Literal["workflow", "skill"] = "workflow"
    id: int | None = None
    name: str = Field(default="", max_length=200)
    kind: Literal["image", "video"] = "image"
    # skill mentions
    description: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _check_shape(self) -> "MessageMention":
        if self.type == "workflow" and self.id is None:
            raise ValueError("workflow mention requires id")
        if self.type == "skill" and not self.name:
            raise ValueError("skill mention requires name")
        return self


class MessageAttachment(BaseModel):
    path: str = Field(min_length=1, max_length=2000)
    name: str = Field(default="", max_length=500)
    kind: Literal["image", "video", "audio"] = "image"


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=500)
    scope: Literal["global", "project"] = "global"
    project_id: int | None = None
    kind: Literal["preference", "convention", "correction"] = "preference"
    session_id: int | None = None


class MessageCreate(BaseModel):
    content: str = ""
    mentions: list[MessageMention] = Field(default_factory=list, max_length=1)
    attachments: list[MessageAttachment] = Field(default_factory=list, max_length=8)
    answer_to: int | None = None  # question/asked seq this message answers (card click)

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
            answer_to=payload.answer_to,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "message": user_msg}


@router.post("/sessions/{session_id}/cancel")
async def cancel_session(session_id: int) -> dict[str, Any]:
    """Stop = stop everything this session started: the LLM turn AND the GPU
    jobs it enqueued. Agent jobs stamp session_id into their payload, so the
    queue can find and cancel them; the worker's poll loop sees the flipped
    row and interrupts the running ComfyUI prompt."""
    from calliope.queue.manager import queue_manager

    cancelled = await runner.cancel(session_id)
    job_ids = queue_manager.cancel_by_session(session_id)
    return {"ok": cancelled or bool(job_ids), "cancelled_jobs": job_ids}


@router.get("/memories")
async def list_memories_admin() -> list[dict[str, Any]]:
    """All memories across scopes/projects for the Settings → Agent page."""
    from calliope.db import get_db, row_to_dict

    conn = get_db(settings.db_path)
    try:
        rows = conn.execute(
            """
            SELECT m.*, p.title AS project_title FROM agent_memory m
            LEFT JOIN projects p ON p.id = m.project_id
            ORDER BY m.scope, m.project_id, m.id DESC
            """
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = row_to_dict(r)
        out.append(
            {
                "id": d["id"],
                "scope": d["scope"],
                "project_id": d["project_id"],
                "project_title": d.get("project_title"),
                "content": d["content"],
                "kind": d["kind"],
                "source": d["source"],
                "use_count": d["use_count"],
                "created_at": d["created_at"],
            }
        )
    return out


@router.post("/memories")
async def add_memory(payload: MemoryCreate) -> dict[str, Any]:
    """User-authored memory (source='user') from the Settings page."""
    from calliope.agent.harness import log as session_log
    from calliope.db import get_db

    content = payload.content.strip()
    if not content or len(content) > 500:
        raise HTTPException(status_code=422, detail="content must be 1-500 chars")
    if payload.scope not in ("global", "project"):
        raise HTTPException(status_code=422, detail="scope must be global or project")
    if payload.kind not in ("preference", "convention", "correction"):
        raise HTTPException(status_code=422, detail="invalid kind")
    conn = get_db(settings.db_path)
    try:
        if payload.scope == "project" and payload.project_id is not None:
            if not conn.execute(
                "SELECT id FROM projects WHERE id = ?", (payload.project_id,)
            ).fetchone():
                raise HTTPException(status_code=404, detail="Project not found")
        cur = conn.execute(
            "INSERT INTO agent_memory (scope, project_id, content, kind, source) "
            "VALUES (?, ?, ?, ?, 'user')",
            (payload.scope, payload.project_id, content, payload.kind),
        )
        conn.commit()
        mem_id = int(cur.lastrowid)
    finally:
        conn.close()
    if payload.session_id:
        session_log.append_event(
            payload.session_id,
            session_log.MEMORY_SAVED,
            {"memory_id": mem_id, "scope": payload.scope, "kind": payload.kind, "content": content},
        )
    return {"ok": True, "id": mem_id}


@router.delete("/memories/{memory_id}")
async def delete_memory(memory_id: int) -> dict[str, Any]:
    from calliope.db import get_db

    conn = get_db(settings.db_path)
    try:
        cur = conn.execute("DELETE FROM agent_memory WHERE id = ?", (memory_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Memory not found")
    finally:
        conn.close()
    return {"ok": True}


@router.get("/skills")
async def list_skills() -> list[dict[str, Any]]:
    """Frontmatter-only skill list for the composer's `/` typeahead (same
    view the agent's list_skills tool sees; no file bodies)."""
    from calliope.agent.skills_store import list_skills as store_list_skills

    return store_list_skills()


@router.get("/skills/path")
async def skills_path() -> dict[str, Any]:
    """On-disk skills folder for the Settings → Skills page."""
    from calliope.agent.skills_store import skills_root

    return {"path": str(skills_root())}


@router.get("/skills/{name}/files")
async def skill_files(name: str) -> dict[str, Any]:
    from calliope.agent.skills_store import skill_files as store_skill_files

    files = store_skill_files(name)
    if files is None:
        raise HTTPException(status_code=404, detail=f"Unknown skill: {name}")
    return {"skill": name, "files": files}


@router.get("/skills/{name}/file")
async def read_skill_file(name: str, path: str = Query(default="SKILL.md")) -> dict[str, Any]:
    from calliope.agent.skills_store import read_skill_file as store_read

    result = store_read(name, path)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error") or "Not found")
    return result
