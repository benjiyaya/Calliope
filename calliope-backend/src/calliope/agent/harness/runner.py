"""Agent session runner: persistence + asyncio task lifecycle.

Runs agent turns as asyncio tasks (LLM-bound work stays off the ComfyUI job
queue, which is render-bound). The session event log is the source of truth:
the loop appends events; the runner derives LLM history from it, backfills
legacy agent_messages sessions on first run, and broadcasts agent.message /
agent.session.updated events.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from calliope.agent.harness import log as session_log
from calliope.agent.harness.orchestrator import orchestrate
from calliope.agent.harness.registry import ToolContext
from calliope.config import settings
from calliope.db import get_db
from calliope.events.bus import event_bus

logger = logging.getLogger("calliope.harness.runner")


class AgentRunner:
    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task] = {}
        # Guards the is_running check → task registration span in start_turn:
        # awaits in between (event publishes, DB writes) would otherwise let a
        # concurrent POST slip past the check and double-run one session.
        self._start_lock = asyncio.Lock()

    # ── persistence ──────────────────────────────────────────────

    def _db(self):
        return get_db(settings.db_path)

    def _set_status(self, session_id: int, status: str) -> None:
        conn = self._db()
        try:
            conn.execute(
                "UPDATE agent_sessions SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, session_id),
            )
            conn.commit()
        finally:
            conn.close()

    async def _publish_session(self, session_id: int) -> None:
        from calliope.routers.agent import session_event_payload

        session = await session_event_payload(session_id)
        if session:
            await event_bus.publish("agent.session.updated", {"session": session})

    def _persist_message(
        self,
        session_id: int,
        *,
        role: str,
        content: str,
        agent_name: str | None = None,
        tool_name: str | None = None,
        tool_args: dict[str, Any] | None = None,
        tool_result: Any = None,
        status: str | None = None,
        append_event: bool = True,
        extra_event_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Mirror into agent_messages (legacy chat rows) + optionally append
        the corresponding session event. The event log is the source of truth;
        tool exchanges are appended by the loop itself (append_event=False in
        the sink to avoid duplicates)."""
        if append_event:
            event_type = {
                "user": session_log.USER_MESSAGE,
                "assistant": session_log.ASSISTANT_MESSAGE,
            }.get(role)
            if event_type is not None:
                data: dict[str, Any] = {
                    "content": content,
                    "agent_name": agent_name,
                    "status": status,
                }
                if extra_event_data:
                    data.update(extra_event_data)
                session_log.append_event(session_id, event_type, data)
        conn = self._db()
        try:
            cur = conn.execute(
                """
                INSERT INTO agent_messages
                (session_id, role, content, agent_name, tool_name, tool_args_json, tool_result_json, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    role,
                    content or "",
                    agent_name,
                    tool_name,
                    json.dumps(tool_args, ensure_ascii=False) if tool_args else None,
                    json.dumps(tool_result, ensure_ascii=False, default=str)
                    if tool_result is not None
                    else None,
                    status,
                ),
            )
            conn.execute(
                "UPDATE agent_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (session_id,),
            )
            conn.commit()
            from calliope.db import row_to_dict

            row = conn.execute(
                "SELECT * FROM agent_messages WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return row_to_dict(row)
        finally:
            conn.close()

    def _make_message_sink(self, session_id: int):
        """Persistence + broadcast sink for harness messages (planner notes,
        sub-agent reports). Tool calls/results are already logged by the loop
        via the event log — the sink only mirrors them into agent_messages."""

        async def sink(message: dict[str, Any]) -> None:
            tool_result = message.get("tool_result")
            if tool_result is not None:
                # Bound the mirrored copy (event log caps its own at 16KB):
                # agent_messages.tool_result_json and the SSE echo must not
                # carry multi-hundred-KB comfy_mcp payloads verbatim.
                text = json.dumps(tool_result, ensure_ascii=False, default=str)
                if len(text) > session_log.EVENT_DATA_TRUNCATE:
                    tool_result = {
                        "truncated": True,
                        "preview": text[: session_log.EVENT_DATA_TRUNCATE] + "…[truncated]",
                    }
            persisted = self._persist_message(
                session_id,
                role=message.get("role") or "assistant",
                content=message.get("content") or "",
                agent_name=message.get("agent_name"),
                tool_name=message.get("tool_name"),
                tool_args=message.get("tool_args")
                if isinstance(message.get("tool_args"), dict)
                else None,
                tool_result=tool_result,
                status=message.get("status"),
                append_event=False,  # the loop already logged these events
            )
            await event_bus.publish("agent.message", {"message": persisted})

        return sink

    # ── session lifecycle ────────────────────────────────────────

    def is_running(self, session_id: int) -> bool:
        task = self._tasks.get(session_id)
        return task is not None and not task.done()

    async def start_turn(
        self,
        session_id: int,
        user_message: str,
        *,
        mentions: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Persist the user message and kick off the agent turn. Returns the
        persisted user message dict. Raises RuntimeError when already running."""
        async with self._start_lock:
            if self.is_running(session_id):
                raise RuntimeError("Session is already running a turn")

            conn = self._db()
            try:
                row = conn.execute(
                    "SELECT * FROM agent_sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not row:
                    raise ValueError("Session not found")
                session = dict(row)
            finally:
                conn.close()

            extra: dict[str, Any] = {}
            if mentions:
                extra["mentions"] = mentions
            if attachments:
                extra["attachments"] = attachments
            user_msg = self._persist_message(
                session_id,
                role="user",
                content=user_message,
                extra_event_data=extra or None,
            )
            if mentions:
                user_msg["mentions"] = mentions
            if attachments:
                user_msg["attachments"] = attachments
            await event_bus.publish("agent.message", {"message": user_msg})

            # Auto-title from the first user message
            if session["title"] in ("New chat", ""):
                title_src = user_message.strip().splitlines()[0] if user_message.strip() else ""
                if not title_src and mentions:
                    title_src = f"@{mentions[0].get('name') or 'workflow'}"
                if not title_src and attachments:
                    title_src = str(attachments[0].get("name") or "Attachments")
                title = (title_src or "New chat")[:60]
                conn = self._db()
                try:
                    conn.execute(
                        "UPDATE agent_sessions SET title = ? WHERE id = ?",
                        (title, session_id),
                    )
                    conn.commit()
                finally:
                    conn.close()

            self._set_status(session_id, "running")
            task = asyncio.create_task(
                self._run_session(session_id), name=f"agent-session-{session_id}"
            )
            self._tasks[session_id] = task
            task.add_done_callback(lambda t: self._tasks.pop(session_id, None))
        # Outside the lock: publish after registration so listeners see the
        # task already registered (running=true).
        await self._publish_session(session_id)
        return user_msg

    async def _run_session(self, session_id: int) -> None:
        conn = self._db()
        try:
            row = conn.execute(
                "SELECT project_id FROM agent_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            project_id = row["project_id"] if row else None
        finally:
            conn.close()

        ctx = ToolContext(session_id=session_id, project_id=project_id)
        try:
            final = await orchestrate(
                ctx,
                [],  # history derived inside from the event log
                session_id=session_id,
                on_message=self._make_message_sink(session_id),
            )
            # Final answer is persisted + broadcast by the loop's emit() call
            # (which goes through the message sink). Only set status here.
            self._set_status(session_id, "idle")
        except asyncio.CancelledError:
            self._persist_message(
                session_id,
                role="assistant",
                content="— run cancelled —",
                status="cancelled",
            )
            self._set_status(session_id, "idle")
            raise
        except Exception as exc:  # noqa: BLE001 — surfaced to the chat as the reply
            logger.exception("Agent turn failed for session %s", session_id)
            self._persist_message(
                session_id,
                role="assistant",
                content=f"Agent error: {exc}",
                status="error",
            )
            self._set_status(session_id, "error")
        finally:
            await self._publish_session(session_id)

    async def cancel(self, session_id: int) -> bool:
        task = self._tasks.get(session_id)
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        return True

    async def shutdown(self) -> None:
        tasks = [t for t in list(self._tasks.values()) if not t.done()]
        for task in tasks:
            task.cancel()
        # Reap cancelled tasks so LLM / comfy-mcp resources actually close
        # instead of leaking (each task's CancelledError is consumed here).
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        # Tear down the shared comfy-mcp subprocess if it was ever spawned.
        from calliope.agent.harness.plugins.comfy_mcp import _client, _get_client  # noqa: F401

        if _client is not None:
            await _client.close()


runner = AgentRunner()
