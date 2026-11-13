"""Durable notes the agent can recall on a later turn."""

from __future__ import annotations

from typing import Any

from calliope.agent.capabilities.sql import as_int, connect
from calliope.db import row_to_dict

_KINDS = {"preference", "convention", "correction"}
_SCOPES = {"global", "project"}
_RECALL_CAP = 12


def save_memory(project_id: int | None, args: dict[str, Any]) -> dict[str, Any]:
    content = str(args.get("content") or "").strip()
    if not content:
        return {"ok": False, "error": "content is required"}
    content = content[:500]
    kind = str(args.get("kind") or "preference")
    if kind not in _KINDS:
        return {"ok": False, "error": "kind must be preference, convention, or correction"}
    scope = str(args.get("scope") or ("project" if project_id is not None else "global"))
    if scope not in _SCOPES:
        return {"ok": False, "error": "scope must be global or project"}
    if scope == "project" and project_id is None:
        return {"ok": False, "error": "a project memory needs a linked project"}
    stored_project = project_id if scope == "project" else None
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO agent_memory (scope, project_id, content, kind, source)
            VALUES (?, ?, ?, ?, 'agent')
            ON CONFLICT(scope, project_id, content) DO UPDATE SET
                kind = excluded.kind
            """,
            (scope, stored_project, content, kind),
        )
        conn.commit()
        return {"ok": True, "scope": scope, "content": content}
    finally:
        conn.close()


def list_memories(project_id: int | None, args: dict[str, Any] | None = None) -> dict[str, Any]:
    del args
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT id, scope, project_id, content, kind
            FROM agent_memory
            WHERE scope = 'global' OR project_id = ?
            ORDER BY use_count DESC, id DESC
            LIMIT ?
            """,
            (project_id, _RECALL_CAP),
        ).fetchall()
        return {"ok": True, "memories": [row_to_dict(row) for row in rows]}
    finally:
        conn.close()


def forget_memory(project_id: int | None, args: dict[str, Any]) -> dict[str, Any]:
    memory_id = as_int(args.get("memory_id") or args.get("id"))
    content = str(args.get("content") or "").strip()
    conn = connect()
    try:
        if memory_id is not None:
            cur = conn.execute(
                """
                DELETE FROM agent_memory
                WHERE id = ? AND (scope = 'global' OR project_id = ?)
                """,
                (memory_id, project_id),
            )
        elif content:
            cur = conn.execute(
                """
                DELETE FROM agent_memory
                WHERE content = ? AND (scope = 'global' OR project_id = ?)
                """,
                (content, project_id),
            )
        else:
            return {"ok": False, "error": "pass memory_id or content"}
        conn.commit()
        if cur.rowcount == 0:
            return {"ok": False, "error": "memory not found"}
        return {"ok": True, "deleted": cur.rowcount}
    finally:
        conn.close()


def recall_text(project_id: int | None) -> str:
    """Short block for the system prompt. Bumps use_count for what was shown."""
    listed = list_memories(project_id)
    rows = listed.get("memories") or []
    if not rows:
        return ""
    conn = connect()
    try:
        for row in rows:
            conn.execute(
                """
                UPDATE agent_memory
                SET use_count = use_count + 1, last_used_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (row["id"],),
            )
        conn.commit()
    finally:
        conn.close()
    lines = [f"- ({row['scope']}/{row['kind']}) {row['content']}" for row in rows]
    return "Memories:\n" + "\n".join(lines)
