"""One Run per user message. The runner owns cancellation and the session row."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from calliope.agent.runtime.main_agent import TurnResult, handle
from calliope.agent.runtime.types import StreamSink


async def execute(
    session_id: int,
    project_id: int | None,
    goal: str,
    *,
    origin: str = "chat",
    thinking: str | None = None,
    cancel: asyncio.Event | None = None,
    pull_steering: Callable[[], list[str]] | None = None,
    on_tool: Callable[[str, dict[str, Any], dict[str, Any]], Any] | None = None,
    on_plan: Callable[[list[dict[str, Any]], str], Any] | None = None,
    on_task: Callable[..., Any] | None = None,
    stream: StreamSink | None = None,
    attachments: list[dict[str, Any]] | None = None,
    mentions: list[dict[str, Any]] | None = None,
) -> TurnResult:
    return await handle(
        session_id,
        project_id,
        goal,
        origin=origin,
        thinking=thinking,
        cancel=cancel,
        pull_steering=pull_steering,
        on_tool=on_tool,
        on_plan=on_plan,
        on_task=on_task,
        stream=stream,
        attachments=attachments,
        mentions=mentions,
    )
