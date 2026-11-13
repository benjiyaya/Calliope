"""_StreamPublisher: throttled SSE batching for the agent loop's stream.

The llm client invokes token()/reasoning()/usage() synchronously inside its
stream loop; the publisher buffers and drains on an 80ms task. These tests
pin the batching, the role prefix, the usage accumulation, and the empty
flush tail.
"""

from __future__ import annotations

import asyncio

import pytest

from calliope.agent.harness.runner import _StreamPublisher
from calliope.events import bus as bus_mod


@pytest.fixture
def published(monkeypatch) -> list[tuple[str, dict]]:
    """Capture event_bus.publish calls (the runner module holds the same singleton)."""
    out: list[tuple[str, dict]] = []

    async def fake_publish(name: str, data: dict) -> None:
        out.append((name, dict(data)))

    monkeypatch.setattr(bus_mod.event_bus, "publish", fake_publish)
    return out


@pytest.mark.asyncio
async def test_token_burst_publishes_one_joined_event(published):
    pub = _StreamPublisher(42)
    for ch in "hello world":
        pub.token(ch)
    await pub.flush()
    # The trailing timer task flushes nothing extra — await it to prove it.
    if pub._task is not None:
        await pub._task
    token_events = [d for (name, d) in published if name == "agent.token"]
    assert len(token_events) == 1
    assert token_events[0]["content"] == "hello world"
    assert token_events[0]["session_id"] == 42


@pytest.mark.asyncio
async def test_reasoning_carries_the_running_grant(published):
    pub = _StreamPublisher(7)
    pub.role = "assets"
    pub.reasoning("looking at the roster")
    await pub.flush()
    pub.role = "script"
    pub.reasoning(" and then the scenes")
    await pub.flush()
    thinking = [d for (name, d) in published if name == "agent.thinking"]
    assert [d["agent_name"] for d in thinking] == ["assets", "script"]
    assert thinking[0]["content"] == "looking at the roster"
    assert thinking[1]["content"] == " and then the scenes"


@pytest.mark.asyncio
async def test_usage_accumulates_and_ignores_garbage(published):
    pub = _StreamPublisher(1)
    pub.usage({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
    pub.usage({"prompt_tokens": 2.0, "completion_tokens": 3, "note": "x"})
    pub.usage({"prompt_tokens": "not-a-number"})
    assert pub.usage_totals == {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 15}


@pytest.mark.asyncio
async def test_flush_with_no_deltas_publishes_nothing(published):
    pub = _StreamPublisher(3)
    pub.usage({"total_tokens": 4})  # usage alone must not schedule a flush
    await pub.flush()
    assert published == []


@pytest.mark.asyncio
async def test_flush_drains_both_buffers(published):
    pub = _StreamPublisher(9)
    pub.token("answer ")
    pub.reasoning("thought ")
    await pub.flush()
    names = sorted(name for name, _ in published)
    assert names == ["agent.thinking", "agent.token"]
