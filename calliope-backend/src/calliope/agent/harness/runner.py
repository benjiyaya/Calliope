"""Session adapter. Routers keep calling this object; the run lives in runtime."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from calliope.agent.harness import log as session_log
from calliope.agent.runtime.run_service import execute
from calliope.agent.runtime.types import StreamSink
from calliope.config import settings
from calliope.db import get_db, row_to_dict
from calliope.events.bus import event_bus

logger = logging.getLogger("calliope.agent")


class _StreamPublisher(StreamSink):
    """Batches LLM stream deltas into throttled SSE publishes.

    The llm client invokes token()/reasoning()/usage() synchronously inside
    its stream loop, while event_bus.publish is async — so deltas buffer and
    drain on an 80ms task (one SSE event per window instead of per token).
    flush() drains the tail before the turn settles so nothing strands after
    the final assistant message. Usage reports accumulate for the turn and
    land on the assistant/message event data.
    """

    FLUSH_DELAY = 0.08

    def __init__(self, session_id: int) -> None:
        self._session_id = session_id
        self.role: str | None = None
        self._tokens: list[str] = []
        self._reasoning: list[str] = []
        self.usage_totals: dict[str, int] = {}
        self._task: asyncio.Task[None] | None = None

    def token(self, content: str) -> None:
        self._tokens.append(content)
        self._schedule()

    def reasoning(self, content: str) -> None:
        self._reasoning.append(content)
        self._schedule()

    def usage(self, usage: dict[str, Any]) -> None:
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)):
                self.usage_totals[key] = self.usage_totals.get(key, 0) + int(value)

    def _schedule(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._flush_soon())

    async def _flush_soon(self) -> None:
        await asyncio.sleep(self.FLUSH_DELAY)
        await self.flush()

    async def flush(self) -> None:
        if self._tokens:
            joined = "".join(self._tokens)
            self._tokens.clear()
            await event_bus.publish(
                "agent.token",
                {"session_id": self._session_id, "content": joined},
            )
        if self._reasoning:
            joined = "".join(self._reasoning)
            self._reasoning.clear()
            await event_bus.publish(
                "agent.thinking",
                {
                    "session_id": self._session_id,
                    "agent_name": self.role,
                    "content": joined,
                },
            )


class Runner:
    def __init__(self) -> None:
        self._running: set[int] = set()
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._cancel: dict[int, asyncio.Event] = {}
        self._steer_seq: dict[int, int] = {}
        self._streams: dict[int, _StreamPublisher] = {}

    def is_running(self, session_id: int) -> bool:
        return session_id in self._running

    def recover_orphaned_sessions(self) -> None:
        """A process restart leaves no live task. Those rows go back to idle."""
        conn = get_db(settings.db_path)
        try:
            conn.execute(
                "UPDATE agent_sessions SET status = 'idle' WHERE status = 'running'"
            )
            conn.commit()
        finally:
            conn.close()

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for event in self._cancel.values():
            event.set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._cancel.clear()
        self._running.clear()

    async def start_turn(
        self,
        session_id: int,
        content: str,
        *,
        mentions: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        answer_to: int | None = None,
        thinking: str | None = None,
    ) -> dict[str, Any]:
        if session_id in self._running:
            raise RuntimeError("Session is running")
        conn = get_db(settings.db_path)
        try:
            row = conn.execute(
                "SELECT id FROM agent_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                raise ValueError("Session not found")
            cur = conn.execute(
                """
                INSERT INTO agent_messages (session_id, role, content, status)
                VALUES (?, 'user', ?, NULL)
                """,
                (session_id, content),
            )
            conn.execute(
                """
                UPDATE agent_sessions
                SET status = 'running', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (session_id,),
            )
            conn.commit()
            stored = conn.execute(
                "SELECT * FROM agent_messages WHERE id = ?", (int(cur.lastrowid),)
            ).fetchone()
            message = row_to_dict(stored)
        finally:
            conn.close()
        message["mentions"] = mentions or []
        message["attachments"] = attachments or []
        message["tool_args"] = None
        message["tool_result"] = None
        self._running.add(session_id)
        seq = session_log.append_event(
            session_id,
            "user/message",
            {
                "role": "user",
                "content": content,
                "mentions": mentions or [],
                "attachments": attachments or [],
                "answer_to": answer_to,
                "thinking": thinking,
            },
        )
        self._steer_seq[session_id] = seq
        self._cancel[session_id] = asyncio.Event()
        await self._publish_message(message)
        await self._publish_session(session_id)
        self._tasks[session_id] = asyncio.create_task(
            self._run(session_id, content, thinking, mentions, attachments),
            name=f"calliope-run-{session_id}",
        )
        return message

    async def steer(
        self,
        session_id: int,
        content: str,
        *,
        mentions: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        if session_id not in self._running:
            return None
        conn = get_db(settings.db_path)
        try:
            cur = conn.execute(
                """
                INSERT INTO agent_messages (session_id, role, content, status)
                VALUES (?, 'user', ?, 'steering')
                """,
                (session_id, content),
            )
            conn.commit()
            stored = conn.execute(
                "SELECT * FROM agent_messages WHERE id = ?", (int(cur.lastrowid),)
            ).fetchone()
            message = row_to_dict(stored)
        finally:
            conn.close()
        message["mentions"] = mentions or []
        message["attachments"] = attachments or []
        message["tool_args"] = None
        message["tool_result"] = None
        session_log.append_event(
            session_id,
            "steering/message",
            {"role": "user", "content": content, "status": "steering"},
        )
        await self._publish_message(message)
        return message

    async def cancel(self, session_id: int) -> bool:
        task = self._tasks.get(session_id)
        if session_id not in self._running and (task is None or task.done()):
            return False
        event = self._cancel.get(session_id)
        if event is not None:
            event.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._running.discard(session_id)
        self._tasks.pop(session_id, None)
        self._cancel.pop(session_id, None)
        self._set_status(session_id, "idle")
        await self._publish_session(session_id)
        return True

    async def _run(
        self,
        session_id: int,
        content: str,
        thinking: str | None = None,
        mentions: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> None:
        status = "idle"
        sink = _StreamPublisher(session_id)
        self._streams[session_id] = sink
        try:
            project_id, origin = self._scope(session_id)
            result = await execute(
                session_id,
                project_id,
                content,
                origin=origin,
                thinking=thinking,
                cancel=self._cancel.get(session_id),
                pull_steering=lambda: self._drain_steering(session_id),
                on_tool=lambda name, args, tool_result: self._on_tool(
                    session_id, name, args, tool_result
                ),
                on_plan=lambda tasks, note: self._on_plan(session_id, tasks, note),
                on_task=lambda index, task_status, role="script": self._on_task(
                    session_id, index, task_status, role
                ),
                stream=sink,
                attachments=attachments,
                mentions=mentions,
            )
            await sink.flush()
            await self._store_assistant(
                session_id, result.text, agent_name=None, usage=sink.usage_totals or None
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("agent run failed session=%s", session_id)
            status = "error"
            await self._store_assistant(
                session_id,
                "The run failed before a scene check completed.",
                agent_name=None,
            )
        finally:
            try:
                await sink.flush()
            except Exception:  # noqa: BLE001 - SSE tail must not mask the outcome
                logger.exception("stream flush failed session=%s", session_id)
            self._running.discard(session_id)
            self._tasks.pop(session_id, None)
            self._cancel.pop(session_id, None)
            self._streams.pop(session_id, None)
            self._set_status(session_id, status)
            await self._publish_session(session_id)

    def _drain_steering(self, session_id: int) -> list[str]:
        notes, seq = session_log.steering_after(
            session_id, self._steer_seq.get(session_id, 0)
        )
        self._steer_seq[session_id] = seq
        return notes

    def _scope(self, session_id: int) -> tuple[int | None, str]:
        conn = get_db(settings.db_path)
        try:
            row = conn.execute(
                "SELECT project_id, origin FROM agent_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return None, "chat"
            project_id = None if row["project_id"] is None else int(row["project_id"])
            origin = row["origin"] if row["origin"] in ("chat", "scene") else "chat"
            return project_id, origin
        finally:
            conn.close()

    def _set_status(self, session_id: int, status: str) -> None:
        conn = get_db(settings.db_path)
        try:
            conn.execute(
                """
                UPDATE agent_sessions
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, session_id),
            )
            conn.commit()
        finally:
            conn.close()

    async def _on_plan(self, session_id: int, tasks: list[dict[str, Any]], note: str) -> None:
        session_log.append_event(session_id, "plan/created", {"tasks": tasks, "note": note})
        await event_bus.publish(
            "agent.plan", {"session_id": session_id, "tasks": tasks, "note": note}
        )

    async def _on_task(
        self, session_id: int, index: int, status: str, role: str = "script"
    ) -> None:
        sink = self._streams.get(session_id)
        if sink is not None:
            sink.role = role
        event_type = "task/start" if status == "running" else "task/end"
        session_log.append_event(
            session_id,
            event_type,
            {"index": index, "status": status, "role": role},
        )
        await event_bus.publish(
            "agent.task",
            {
                "session_id": session_id,
                "index": index,
                "status": status,
                "role": role,
            },
        )

    async def _on_tool(
        self,
        session_id: int,
        name: str,
        args: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        await event_bus.publish(
            "agent.tool",
            {"session_id": session_id, "phase": "start", "tool": name, "args": args},
        )
        if name == "ask_user" and result.get("ok"):
            # Allocate the question event FIRST so the tool result carries the
            # real seq — the UI's answer_to and the guards' approval scan both
            # match on it (a placeholder 0 would match nothing).
            seq = session_log.append_event(
                session_id,
                "question/asked",
                {
                    "question": result.get("question"),
                    "options": result.get("options"),
                    "scope": result.get("scope"),
                },
            )
            result["question_seq"] = seq
        content = json.dumps(result)[:2000]
        conn = get_db(settings.db_path)
        try:
            conn.execute(
                """
                INSERT INTO agent_messages
                    (session_id, role, content, tool_name, tool_args_json, tool_result_json)
                VALUES (?, 'tool', ?, ?, ?, ?)
                """,
                (session_id, content, name, json.dumps(args), json.dumps(result)),
            )
            conn.commit()
        finally:
            conn.close()
        session_log.append_event(
            session_id,
            "tool/result",
            {
                "role": "tool",
                "content": content,
                "tool_name": name,
                "tool_args": args,
                "tool_result": result,
                "agent_name": "script",
            },
        )
        await event_bus.publish(
            "agent.tool",
            {
                "session_id": session_id,
                "phase": "finish",
                "tool": name,
                "args": args,
                "result": result,
            },
        )

    async def _store_assistant(
        self,
        session_id: int,
        content: str,
        *,
        agent_name: str | None,
        usage: dict[str, int] | None = None,
    ) -> None:
        conn = get_db(settings.db_path)
        try:
            cur = conn.execute(
                """
                INSERT INTO agent_messages (session_id, role, content, agent_name)
                VALUES (?, 'assistant', ?, ?)
                """,
                (session_id, content, agent_name),
            )
            conn.commit()
            stored = conn.execute(
                "SELECT * FROM agent_messages WHERE id = ?", (int(cur.lastrowid),)
            ).fetchone()
            message = row_to_dict(stored)
        finally:
            conn.close()
        message["tool_args"] = None
        message["tool_result"] = None
        message["mentions"] = []
        message["attachments"] = []
        event_data: dict[str, Any] = {
            "role": "assistant",
            "content": content,
            "agent_name": agent_name,
        }
        if usage:
            # Reported by the endpoint on the stream; never fabricated.
            event_data["usage"] = usage
        session_log.append_event(session_id, "assistant/message", event_data)
        await self._publish_message(message)

    async def _publish_message(self, message: dict[str, Any]) -> None:
        await event_bus.publish("agent.message", {"message": message})

    async def _publish_session(self, session_id: int) -> None:
        conn = get_db(settings.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM agent_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if not row:
                return
            session = row_to_dict(row)
            if session.get("project_id"):
                project = conn.execute(
                    "SELECT id, title, status FROM projects WHERE id = ?",
                    (session["project_id"],),
                ).fetchone()
                session["project"] = row_to_dict(project) if project else None
            else:
                session["project"] = None
        finally:
            conn.close()
        session["running"] = self.is_running(session_id)
        await event_bus.publish("agent.session.updated", {"session": session})


runner = Runner()
