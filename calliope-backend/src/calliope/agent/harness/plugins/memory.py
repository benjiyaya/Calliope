"""Memory plugin: durable user preferences + project conventions.

Three tools (save / list / forget) and one recall path: the prompt section
renders the top-N usage-ranked memories into the system prompt on every step
(loop.py already re-assembles per step, so mid-turn saves are visible to the
agent's own next step). Memories are agent-authored, user-visible, and
user-deletable (Settings → Agent → Memory).
"""
from __future__ import annotations

import logging
from typing import Any

from calliope.agent.harness import log as session_log
from calliope.agent.harness.registry import ToolContext, ToolDefinition, ToolRegistry, _db

logger = logging.getLogger("calliope.harness.plugins.memory")

CONTENT_CAP = 500
PROMPT_CHAR_BUDGET = 1500
PROMPT_ITEM_CAP = 12
_ALLOWED_KINDS = ("preference", "convention", "correction")


async def t_save_memory(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    content = str(args.get("content") or "").strip()
    if not content:
        return {"ok": False, "error": "content is required"}
    if len(content) > CONTENT_CAP:
        return {
            "ok": False,
            "error": f"content must be <= {CONTENT_CAP} chars — one atomic fact, not an essay",
        }
    scope = str(args.get("scope") or ("project" if ctx.project_id else "global")).strip()
    if scope not in ("global", "project"):
        return {"ok": False, "error": "scope must be 'global' or 'project'"}
    if scope == "project":
        if ctx.project_id is None:
            return {
                "ok": False,
                "error": "scope='project' needs a linked project — use scope='global' in a sandbox session",
            }
        project_id: int | None = ctx.project_id
    else:
        project_id = None
    kind = str(args.get("kind") or "preference").strip()
    if kind not in _ALLOWED_KINDS:
        return {"ok": False, "error": f"kind must be one of {list(_ALLOWED_KINDS)}"}

    conn = _db()
    try:
        existing = conn.execute(
            "SELECT id FROM agent_memory WHERE scope = ? AND project_id IS ? AND content = ?",
            (scope, project_id, content),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE agent_memory SET last_used_at = CURRENT_TIMESTAMP WHERE id = ?",
                (existing["id"],),
            )
            conn.commit()
            return {"ok": True, "memory_id": existing["id"], "deduplicated": True}
        cur = conn.execute(
            "INSERT INTO agent_memory (scope, project_id, content, kind, source) VALUES (?, ?, ?, ?, 'agent')",
            (scope, project_id, content, kind),
        )
        memory_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    session_log.append_event(
        ctx.session_id,
        session_log.MEMORY_SAVED,
        {"memory_id": memory_id, "scope": scope, "kind": kind, "content": content},
    )
    return {"ok": True, "memory_id": memory_id, "scope": scope, "kind": kind, "content": content}


async def t_list_memories(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    scope_filter = args.get("scope")
    conn = _db()
    try:
        clauses = ["(scope = 'global' OR project_id = ?)"]
        params: list[Any] = [ctx.project_id]
        if scope_filter in ("global", "project"):
            clauses.append("scope = ?")
            params.append(scope_filter)
        rows = conn.execute(
            f"SELECT * FROM agent_memory WHERE {' AND '.join(clauses)} "
            "ORDER BY use_count DESC, last_used_at DESC, id DESC",
            params,
        ).fetchall()
    finally:
        conn.close()
    return {
        "ok": True,
        "memories": [
            {
                "id": r["id"],
                "scope": r["scope"],
                "content": r["content"],
                "kind": r["kind"],
                "use_count": r["use_count"],
            }
            for r in rows
        ],
        "count": len(rows),
    }


async def t_forget_memory(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    raw = args.get("memory_id")
    try:
        memory_id = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return {"ok": False, "error": "memory_id must be an integer (from list_memories)"}
    conn = _db()
    try:
        row = conn.execute(
            "SELECT id, content FROM agent_memory WHERE id = ? AND (scope = 'global' OR project_id = ?)",
            (memory_id, ctx.project_id),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"no visible memory with id {memory_id}"}
        conn.execute("DELETE FROM agent_memory WHERE id = ?", (memory_id,))
        conn.commit()
    finally:
        conn.close()
    session_log.append_event(
        ctx.session_id,
        session_log.MEMORY_FORGOTTEN,
        {"memory_id": memory_id, "content": row["content"]},
    )
    return {"ok": True, "forgotten": memory_id, "content": row["content"]}


def memory_prompt_text(ctx: ToolContext) -> str | None:
    """System-prompt section body: usage-ranked memories within a char budget.

    Rendering IS usage — the ids actually shown get use_count/last_used_at
    bumped, so ranking measures what the agent sees, not what merely exists.
    """
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM agent_memory WHERE scope = 'global' OR project_id = ? "
            "ORDER BY use_count DESC, last_used_at DESC, id DESC LIMIT ?",
            (ctx.project_id, PROMPT_ITEM_CAP * 2),
        ).fetchall()
        used_ids: list[int] = []
        lines: list[str] = []
        budget = PROMPT_CHAR_BUDGET
        for row in rows:
            if len(lines) >= PROMPT_ITEM_CAP:
                break
            line = f"- [{row['kind']}] {row['content']}"
            if len(line) > budget:
                break
            lines.append(line)
            budget -= len(line)
            used_ids.append(row["id"])
        if not lines:
            return None
        if used_ids:
            conn.execute(
                "UPDATE agent_memory SET use_count = use_count + 1, last_used_at = CURRENT_TIMESTAMP "
                f"WHERE id IN ({','.join('?' for _ in used_ids)})",
                used_ids,
            )
            conn.commit()
    finally:
        conn.close()
    return "## Memory\nDurable user preferences and project conventions — follow them:\n" + "\n".join(lines)


async def _memory_section(ctx: ToolContext) -> str | None:
    return memory_prompt_text(ctx)


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="save_memory",
            description=(
                "Save a durable user preference or project convention for future "
                "sessions — e.g. 'user wants terse scene descriptions', 'this "
                "project never shows the antagonist in daylight'. One atomic fact "
                f"(<= {CONTENT_CAP} chars). Do NOT save one-off task details. If it "
                "contradicts an existing memory, forget the old one first. "
                "scope: 'project' ties it to this project (default when linked), "
                "'global' applies to every project and sandbox sessions."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The fact/preference, one sentence"},
                    "scope": {
                        "type": "string",
                        "enum": ["global", "project"],
                        "description": "Default: project when linked, global in sandbox",
                    },
                    "kind": {
                        "type": "string",
                        "enum": list(_ALLOWED_KINDS),
                        "description": "preference | convention | correction",
                    },
                },
                "required": ["content"],
            },
            executor=t_save_memory,
            category="memory",
            requires_project=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="list_memories",
            description="List saved memories visible to this session (global + this project).",
            parameters={
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["global", "project"]},
                },
            },
            executor=t_list_memories,
            category="memory",
            requires_project=False,
        )
    )
    registry.register(
        ToolDefinition(
            name="forget_memory",
            description=(
                "Delete a memory by id (from list_memories) — use when it is wrong "
                "or superseded by a newer instruction."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "integer"},
                },
                "required": ["memory_id"],
            },
            executor=t_forget_memory,
            category="memory",
            requires_project=False,
        )
    )
