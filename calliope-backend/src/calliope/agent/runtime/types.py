"""Task, Run, and Call.

Mirrors the CoworkOS runtime split. A Call is one delegated assignment.
``succeeded`` means the coordinator accepted the outcome. The model stopping
is a separate fact, recorded on the subagent result, and never written here
by itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

CallStatus = Literal["queued", "running", "blocked", "succeeded", "failed"]


class StreamSink:
    """Where an LLM's stream deltas go while a call runs.

    The llm client invokes these synchronously inside its stream loop, so the
    methods must not raise and must not await — implementations batch and
    schedule their own async work (runner._StreamPublisher). The base sink
    drops everything; `role` is the currently running grant, set from
    task/start, so SSE consumers can prefix reasoning per agent.
    """

    role: str | None = None

    def token(self, content: str) -> None:
        """Assistant text delta."""

    def reasoning(self, content: str) -> None:
        """Reasoning/thinking delta."""

    def usage(self, usage: dict[str, Any]) -> None:
        """Usage report {prompt_tokens, completion_tokens, total_tokens}."""


@dataclass
class Task:
    id: str
    run_id: str
    role: str
    goal: str


@dataclass
class Run:
    id: str
    session_id: int
    project_id: int | None
    goal: str
    status: Literal["running", "completed", "failed", "cancelled"] = "running"


@dataclass
class Call:
    id: str
    task_id: str
    run_id: str
    agent_id: str
    goal: str
    status: CallStatus = "queued"
    attempt: int = 0
    tools_ran: list[str] = field(default_factory=list)
    summary: str = ""
