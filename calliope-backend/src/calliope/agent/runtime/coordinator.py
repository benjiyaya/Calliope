"""Dispatch one scoped call and decide its status from the database.

The model's last sentence is not a status. A write succeeds when the grant's
tables changed. A read succeeds when the mechanical gate is ready. A miss
retries twice with the recovery summary.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from typing import Any

from calliope.agent.capabilities.catalog import empty_story_fingerprint, fingerprint, live_stat
from calliope.agent.capabilities.scenes import list_scene_heads
from calliope.agent.runtime.recovery import subagent_recovery_summary
from calliope.agent.runtime.subagent import SubagentResult, run_subagent
from calliope.agent.runtime.task_completion import assess_task_completion
from calliope.agent.runtime.types import Call, Run, StreamSink, Task
from calliope.config import settings
from calliope.db import get_db

# First try plus two retries. The call stays running until the last one settles.
MAX_ATTEMPTS = 3


def _pending_session_jobs(session_id: int) -> int:
    """Count pending/running jobs stamped with this session.

    Agent enqueues stamp `session_id` into the payload (the same key
    queue_manager.cancel_by_session scans). A read-call for assets/render is
    not "ready" while the jobs it just asked about are still in flight.
    """
    conn = get_db(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM jobs WHERE status IN ('pending', 'running')"
        ).fetchall()
        count = 0
        for r in rows:
            try:
                payload = json.loads(r["payload_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if payload.get("session_id") == session_id:
                count += 1
        return count
    finally:
        conn.close()


def _trim_history(history: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    """Drop oldest complete exchanges until the retry transcript fits the budget.

    A transcript is [system?] + ReAct exchanges (assistant tool_calls + their
    tool rows) + the trailing recovery note. Never splits an assistant/tool
    pair and never drops the trailing row (the loop stops one short of the
    end); the output is therefore never empty even when a single exchange
    exceeds the whole budget — that pair is dropped whole.
    """

    def _len(items: list[dict[str, Any]]) -> int:
        return sum(
            len(str(m.get("content") or ""))
            + len(json.dumps(m.get("tool_calls") or [], default=str))
            for m in items
        )

    if max_chars <= 0 or _len(history) <= max_chars or len(history) <= 2:
        return history
    start = 1  # index 0 is the system message; keep it
    n = len(history)
    while start < n - 1 and _len(history[start:]) > max_chars:
        item = history[start]
        if item.get("role") == "assistant" and item.get("tool_calls"):
            start += 1
            while start < n and history[start].get("role") == "tool":
                start += 1
        else:
            start += 1
    return history[:1] + history[start:]


async def dispatch(
    run: Run,
    goal: str,
    *,
    grant: str = "script",
    write_required: bool,
    required_tool: str | None = None,
    thinking: str | None = None,
    cancel: asyncio.Event | None = None,
    pull_steering: Callable[[], list[str]] | None = None,
    on_tool: Callable[[str, dict[str, Any], dict[str, Any]], Any] | None = None,
    stream: StreamSink | None = None,
    attachments: list[dict[str, Any]] | None = None,
    mentions: list[dict[str, Any]] | None = None,
) -> tuple[Call, list[dict[str, Any]]]:
    # A sandbox chat may open a story call so create_project can link it,
    # and canvas calls target the session's sandbox canvas. Every other
    # grant still needs a project that already exists.
    if grant not in ("shot", "story", "canvas") and run.project_id is None:
        raise RuntimeError("a studio call needs a project")
    task = Task(id=uuid.uuid4().hex, run_id=run.id, role=grant, goal=goal)
    call = Call(
        id=uuid.uuid4().hex,
        task_id=task.id,
        run_id=run.id,
        agent_id=grant,
        goal=goal,
        status="running",
    )
    before = fingerprint(run.project_id, grant, run.session_id)
    before_ids: set[int] = set()
    if grant == "script" and run.project_id is not None:
        before_ids = {int(row["id"]) for row in list_scene_heads(run.project_id)}
    prompt = goal
    history: list[dict[str, Any]] | None = None
    accumulated: list[str] = []
    result = SubagentResult(text="", tools_ran=[], finished=False)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if cancel is not None and cancel.is_set():
            raise asyncio.CancelledError
        call.attempt = attempt
        call.status = "running"
        result = await run_subagent(
            project_id=run.project_id,
            session_id=run.session_id,
            grant=grant,
            goal=prompt,
            thinking=thinking,
            cancel=cancel,
            pull_steering=pull_steering,
            on_tool=on_tool,
            history=history,
            stream=stream,
            attachments=attachments,
            mentions=mentions,
        )
        for name in result.tools_ran:
            if name not in accumulated:
                accumulated.append(name)
        call.tools_ran = list(accumulated)
        call.summary = result.text
        if result.linked_project_id and run.project_id is None:
            run.project_id = int(result.linked_project_id)
            if grant == "story":
                before = empty_story_fingerprint(run.project_id)
        if result.paused:
            call.status = "blocked"
            return call, []
        after = fingerprint(run.project_id, grant, run.session_id)
        created: list[dict[str, Any]] = []
        if grant == "script" and run.project_id is not None:
            created = [
                row
                for row in list_scene_heads(run.project_id)
                if int(row["id"]) not in before_ids
            ]
        pending_jobs = (
            _pending_session_jobs(run.session_id)
            if grant in ("assets", "render")
            else 0
        )
        gate = assess_task_completion(
            has_final_answer=result.finished, pending_jobs=pending_jobs
        )
        changed = before != after
        if (write_required and changed) or (gate.ready and not write_required):
            call.status = "succeeded"
            return call, created
        if attempt == MAX_ATTEMPTS:
            break
        label, count = live_stat(run.project_id, grant, run.session_id)
        note = subagent_recovery_summary(
            tools_ran=accumulated,
            required_tool=required_tool if write_required else None,
            scene_count=count,
            finished=result.finished,
            label=label,
        )
        if gate.reason == "pending_jobs":
            note = (
                f"{note}\n{pending_jobs} job(s) are still pending or running. "
                "Call wait_for_jobs to await them, or state the handoff."
            )
        prompt = f"{goal}\n\n{note}"
        history = _trim_history(
            list(result.transcript), int(settings.history_char_budget())
        ) or [
            {"role": "system", "content": ""},
            {"role": "user", "content": goal},
        ]
        history.append({"role": "user", "content": note})
    call.status = "failed"
    call.summary = result.text
    return call, []
