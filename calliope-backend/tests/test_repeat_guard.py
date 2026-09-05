"""Tests for the loop repetition guard (stall-loop breaker).

The canvas/65-class incident: the model planned run_workflow for two
characters but emitted the same update_character call over and over,
burning the whole step budget while narrating "let me actually call
run_workflow now". The guard executes identical calls at most N times per
turn; repeats get the cached real result plus an escalating instruction.
"""
from __future__ import annotations

import asyncio

import pytest

from calliope.agent.harness.loop import run_turn
from calliope.agent.harness.registry import ToolContext, ToolDefinition, ToolRegistry


@pytest.fixture(autouse=True)
def _scratch_db(monkeypatch, tmp_path):
    import calliope.config as config_module

    monkeypatch.setattr(config_module.settings, "data_dir", tmp_path)
    from calliope.db import get_db, migrate_db

    asyncio.run(migrate_db(tmp_path / "calliope.db"))
    conn = get_db(tmp_path / "calliope.db")
    conn.execute("INSERT INTO agent_sessions (title) VALUES ('guard-test')")
    conn.commit()
    conn.close()
    yield


class _FakeStream:
    """Yields one tool call per construction, then a final text delta."""

    def __init__(self, call):
        self._call = call

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        if self._call is not None:
            yield {"type": "tool_call", "tool_call": self._call}
        else:
            yield {"type": "delta", "content": "done"}


class _FakePrompts:
    async def assemble(self, ctx):
        return "system"


def test_repeat_guard_caches_and_refuses(monkeypatch):
    """Identical calls execute twice, then the guard returns the cached real
    result + an instruction instead of re-executing or silently no-op'ing."""
    executed: list[int] = []
    reg = ToolRegistry()

    async def _count(ctx, args):
        executed.append(1)
        return {"ok": True, "n": len(executed)}

    reg.register(
        ToolDefinition(
            name="echo_tool",
            description="count",
            parameters={"type": "object", "properties": {}},
            executor=_count,
            requires_project=False,
        )
    )

    class _Client:
        def __init__(self):
            self.turn = 0

        async def close(self):
            return None

        def chat_stream(self, messages, temperature=0.4, tools=None):
            self.turn += 1
            # The stall pattern: the SAME call every step
            return _FakeStream(
                {
                    "id": f"c{self.turn}",
                    "function": {"name": "echo_tool", "arguments": "{}"},
                }
            )

    from calliope.agent.harness import loop as loop_mod

    monkeypatch.setattr(loop_mod, "get_registry", lambda: reg)
    monkeypatch.setattr(loop_mod, "get_prompts", lambda: _FakePrompts())
    monkeypatch.setattr(loop_mod, "_llm_for_role", lambda role: _Client())
    monkeypatch.setattr(loop_mod, "_next_turn_number", lambda sid: 1)

    ctx = ToolContext(session_id=1, project_id=None)
    history: list = []
    asyncio.run(run_turn(ctx, history, max_iterations=6))
    # 6 steps of the same call: executed exactly 2 (the limit); the rest
    # were served from cache with the guard receipt.
    assert len(executed) == 2, f"expected 2 real executions, got {len(executed)}"
    guard_receipts = [
        m
        for m in history
        if m.get("role") == "tool" and "repeat_guard" in (m.get("content") or "")
    ]
    assert len(guard_receipts) >= 1, "expected repeat_guard receipts in tool results"


def test_different_args_not_guarded(monkeypatch):
    """Distinct arguments are distinct work — the guard only fires on
    identical (tool, args) repeats."""
    calls: list[dict] = []
    reg = ToolRegistry()

    async def _count(ctx, args):
        calls.append(args)
        return {"ok": True, "n": len(calls)}

    reg.register(
        ToolDefinition(
            name="count_tool",
            description="count",
            parameters={
                "type": "object",
                "properties": {"x": {"type": "integer"}},
            },
            executor=_count,
            requires_project=False,
        )
    )

    class _Client:
        def __init__(self):
            self.turn = 0

        async def close(self):
            return None

        def chat_stream(self, messages, temperature=0.4, tools=None):
            self.turn += 1
            return _FakeStream(
                {
                    "id": f"c{self.turn}",
                    "function": {
                        "name": "count_tool",
                        "arguments": json_dumps({"x": self.turn}),
                    },
                }
            )

    def json_dumps(v):
        import json

        return json.dumps(v)

    from calliope.agent.harness import loop as loop_mod

    monkeypatch.setattr(loop_mod, "get_registry", lambda: reg)
    monkeypatch.setattr(loop_mod, "get_prompts", lambda: _FakePrompts())
    monkeypatch.setattr(loop_mod, "_llm_for_role", lambda role: _Client())
    monkeypatch.setattr(loop_mod, "_next_turn_number", lambda sid: 2)

    ctx = ToolContext(session_id=1, project_id=None)
    history: list = []
    asyncio.run(run_turn(ctx, history, max_iterations=4))
    # 4 steps, all with DIFFERENT args → all executed, guard never fires
    assert len(calls) == 4, f"all distinct calls should execute, got {len(calls)}"
