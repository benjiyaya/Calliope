"""Sub-agent loop safety nets — the swarm must not be a second-class citizen.

Covers the four nets ported from run_turn into _run_sub_agent:
repeat guard, fail-streak directive, final-step budget nudge, and ask_user
escalation (which pauses the whole swarm in orchestrate).
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from calliope.agent.harness.registry import ToolContext
from calliope.config import settings
from calliope.db import get_db

@pytest.fixture(autouse=True, scope="module")
def _never_touch_real_db():
    prev = settings.data_dir
    with tempfile.TemporaryDirectory() as tmp:
        settings.data_dir = Path(tmp)
        settings.assets_dir = Path(tmp) / "assets"
        asyncio.run(_migrate(settings.db_path))
        yield
    settings.data_dir = prev


async def _migrate(db_path):
    from calliope.db import migrate_db

    await migrate_db(db_path)


def _mk_session() -> int:
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO agent_sessions (title, project_id) VALUES (?, NULL)",
            ("subagent-guard-test",),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _tc(name: str, args: str = "{}", call_id: str = "c1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


class _ScriptedClient:
    """chat_with_tools that replays scripted responses, one per call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.seen_messages: list[dict] = []

    async def chat_with_tools(self, messages, temperature=0.7, tools=None, tool_choice=None):
        self.seen_messages.append(list(messages))
        if self._responses:
            return self._responses.pop(0)
        return {"role": "assistant", "content": "done", "tool_calls": []}

    async def close(self):
        pass


class _StubRegistry:
    """Registry stub: counts executions, returns scripted results per tool."""

    def __init__(self, results_by_tool=None):
        self.executed: list[tuple[str, dict]] = []
        self.results_by_tool = results_by_tool or {}

    def get(self, name):
        return None

    async def execute(self, ctx, name, args):
        self.executed.append((name, dict(args)))
        r = self.results_by_tool.get(name)
        return dict(r) if isinstance(r, dict) else {"ok": True}


def _install(orch, client, registry):
    """Patch the orchestrator: LLMClient is a FACTORY (called with no args),
    so wrap the scripted client in a lambda."""
    orch.LLMClient = lambda: client
    orch.get_registry = lambda: registry


def test_repeat_guard_blocks_reexecution():
    """After REPEAT_EXECUTE_LIMIT identical calls, the sub-agent gets the
    cached result back with the repeat_guard directive — not another
    execution burning the step budget."""
    import calliope.agent.harness.orchestrator as orch

    sid = _mk_session()
    # Five model turns, all asking for the same tool+args.
    client = _ScriptedClient(
        [{"role": "assistant", "content": None, "tool_calls": [_tc("get_workspace", call_id=f"c{i}")]}
         for i in range(4)]
        + [{"role": "assistant", "content": "gave up", "tool_calls": []}]
    )
    registry = _StubRegistry()
    orig_client, orig_reg = orch.LLMClient, orch.get_registry
    _install(orch, client, registry)
    try:
        final = asyncio.run(
            orch._run_sub_agent(
                ToolContext(session_id=sid, project_id=1),
                [{"role": "user", "content": "do"}],
                ["get_workspace"],
                agent_name="script-agent",
                max_iterations=6,
            )
        )
    finally:
        orch.LLMClient, orch.get_registry = orig_client, orig_reg

    assert final == "gave up"
    # Executed for real only 2 times (limit=2); later repeats intercepted.
    assert len(registry.executed) == 2
    # The directive reached the model on the intercepted call.
    repeat_msgs = [
        m for batch in client.seen_messages
        for m in batch
        if m.get("role") == "tool" and "repeat_guard" in (m.get("content") or "")
    ]
    assert repeat_msgs, "repeat_guard directive never reached the model"


def test_fail_streak_directive_appended():
    """Two not-ok results in a row append the [SYSTEM DIRECTIVE] change-course
    text to the tool result the model sees."""
    import calliope.agent.harness.orchestrator as orch

    sid = _mk_session()
    client = _ScriptedClient(
        [{"role": "assistant", "content": None, "tool_calls": [_tc("run_workflow", call_id="a")]}]
        + [{"role": "assistant", "content": None, "tool_calls": [_tc("run_workflow", call_id="b")]}]
        + [{"role": "assistant", "content": None, "tool_calls": [_tc("get_workspace", call_id="c")]}]
        + [{"role": "assistant", "content": "changed course", "tool_calls": []}]
    )
    registry = _StubRegistry({"run_workflow": {"ok": False, "error": "blocked"}})
    orig_client, orig_reg = orch.LLMClient, orch.get_registry
    _install(orch, client, registry)
    try:
        final = asyncio.run(
            orch._run_sub_agent(
                ToolContext(session_id=sid, project_id=1),
                [{"role": "user", "content": "do"}],
                ["run_workflow", "get_workspace"],
                agent_name="assets-agent",
                max_iterations=6,
            )
        )
    finally:
        orch.LLMClient, orch.get_registry = orig_client, orig_reg

    assert final == "changed course"
    tool_texts = [m.get("content") or "" for batch in client.seen_messages for m in batch if m.get("role") == "tool"]
    assert any("[SYSTEM DIRECTIVE]" in t for t in tool_texts)


def test_final_step_nudge_present():
    """On the last iteration the model is told to wrap up, not start work."""
    import calliope.agent.harness.orchestrator as orch

    sid = _mk_session()
    client = _ScriptedClient(
        [{"role": "assistant", "content": None, "tool_calls": [_tc("get_workspace")]}]
        + [{"role": "assistant", "content": "wrapping", "tool_calls": []}]
    )
    registry = _StubRegistry()
    orig_client, orig_reg = orch.LLMClient, orch.get_registry
    _install(orch, client, registry)
    try:
        final = asyncio.run(
            orch._run_sub_agent(
                ToolContext(session_id=sid, project_id=1),
                [{"role": "user", "content": "do"}],
                ["get_workspace"],
                agent_name="script-agent",
                max_iterations=2,
            )
        )
    finally:
        orch.LLMClient, orch.get_registry = orig_client, orig_reg

    assert final == "wrapping"
    last_batch = client.seen_messages[-1]
    assert any(
        m.get("role") == "user" and "FINAL step" in (m.get("content") or "")
        for m in last_batch
    ), "final-step nudge missing from the last model request"


def test_ask_user_pauses_sub_agent():
    """An ask_user result ends the sub-agent immediately with the Paused
    prefix — no more model calls, no more executions."""
    import calliope.agent.harness.orchestrator as orch

    sid = _mk_session()
    client = _ScriptedClient(
        [{"role": "assistant", "content": None, "tool_calls": [_tc("ask_user", '{"question": "Generate?"}')]}]
        + [{"role": "assistant", "content": "never reached", "tool_calls": []}]
    )
    registry = _StubRegistry(
        {"ask_user": {"ok": True, "awaiting_user_input": True, "question": "Generate?"}}
    )
    orig_client, orig_reg = orch.LLMClient, orch.get_registry
    _install(orch, client, registry)
    try:
        final = asyncio.run(
            orch._run_sub_agent(
                ToolContext(session_id=sid, project_id=1),
                [{"role": "user", "content": "do"}],
                ["ask_user"],
                agent_name="assets-agent",
                max_iterations=6,
            )
        )
    finally:
        orch.LLMClient, orch.get_registry = orig_client, orig_reg

    assert final.startswith("Paused: ")
    assert "Generate?" in final
    # The scripted "never reached" response stayed in the queue — only one
    # model call happened.
    assert len(client.seen_messages) == 1


def test_ask_user_visible_in_every_role():
    """Every ROLE_TOOLS list carries ask_user so a stuck sub-agent can
    escalate instead of spinning."""
    from calliope.agent.harness.orchestrator import ROLE_TOOLS

    for role, tools in ROLE_TOOLS.items():
        assert "ask_user" in tools, f"{role} cannot escalate"


def test_clip_tools_in_swarm_roles():
    from calliope.agent.harness.orchestrator import ROLE_TOOLS

    for tool in ("list_clips", "break_into_shots", "add_clip", "update_clip", "delete_clip"):
        assert tool in ROLE_TOOLS["script"], f"{tool} missing from script role"
    assert "list_clips" in ROLE_TOOLS["video"]


# ── per-tool wall-clock timeout ─────────────────────────────────────────


def test_tool_timeout_kills_hung_tool():
    """A tool slower than agent_tool_timeout_sec returns ok:False with the
    timeout marker instead of stalling the step forever."""
    import asyncio as aio

    from calliope.agent.harness.registry import ToolDefinition, ToolRegistry

    async def _hang(ctx, args):
        await aio.sleep(30)
        return {"ok": True}

    async def _fast(ctx, args):
        return {"ok": True}

    reg = ToolRegistry()
    reg.register(
        ToolDefinition(name="hang", description="", parameters={}, executor=_hang, requires_project=False)
    )
    reg.register(
        ToolDefinition(
            name="exempt",
            description="",
            parameters={},
            executor=_hang,
            requires_project=False,
            long_running=True,
        )
    )
    reg.register(
        ToolDefinition(name="fast", description="", parameters={}, executor=_fast, requires_project=False)
    )

    orig = settings.agent_tool_timeout_sec
    try:
        settings.agent_tool_timeout_sec = 0.2
        out = aio.run(reg.execute(ToolContext(session_id=1), "hang", {}))
        assert out["ok"] is False
        assert out.get("timeout") is True
        assert "timed out" in out["error"]

        # long_running tools are exempt (they own their wait contract).
        # (Not awaiting the hang: exemption means wait_for is not applied at
        # all — verified structurally by _tool_timeout_sec + the fast path.)
        out_fast = aio.run(reg.execute(ToolContext(session_id=1), "fast", {}))
        assert out_fast["ok"] is True

        # 0 disables the timeout entirely.
        settings.agent_tool_timeout_sec = 0
        from calliope.agent.harness.registry import _tool_timeout_sec

        assert _tool_timeout_sec() == 0.0
    finally:
        settings.agent_tool_timeout_sec = orig


def test_wait_for_jobs_is_long_running():
    """wait_for_jobs owns its timeout contract (timeout_sec →
    queue_poll_timeout_sec) — it must be exempt from the wall-clock cap."""
    from calliope.agent.harness.tools import TOOLS

    assert TOOLS["wait_for_jobs"].long_running is True


# ── planner fast-path ───────────────────────────────────────────────────


def test_is_trivial_goal_classification():
    from calliope.agent.harness.orchestrator import _is_trivial_goal

    assert _is_trivial_goal("rename character 3 to Kira")
    assert _is_trivial_goal("What's the queue status?")
    assert _is_trivial_goal("show me scene 2")
    assert _is_trivial_goal("LIST WORKFLOWS")
    # Multi-step / long / procedural messages must NOT skip the planner.
    assert not _is_trivial_goal("draft a new storyline and then regenerate the script")
    assert not _is_trivial_goal("")
    assert not _is_trivial_goal("x" * 200)
    assert not _is_trivial_goal("first do this;\nthen do that")


def test_trivial_goal_skips_planner():
    """A one-line imperative goes straight to run_turn — no planner LLM call,
    no workspace read."""
    import calliope.agent.harness.orchestrator as orch

    sid = _mk_session()
    from calliope.agent.harness import log as session_log

    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "rename character 3 to Kira"})

    planner_calls = {"n": 0}

    class _PlannerSpy:
        def __init__(self):
            planner_calls["n"] += 1

        async def chat(self, *a, **kw):
            raise AssertionError("planner must not be called for a trivial goal")

        async def close(self):
            return None

    async def fake_run_turn(ctx, history, *, on_message=None):
        return "single-loop answer"

    orig_client = orch.LLMClient
    orig_run_turn = orch.run_turn
    orig_ws = orch.get_registry
    orch.LLMClient = _PlannerSpy
    orch.run_turn = fake_run_turn

    class _NoWorkspace:
        def get(self, name):
            return None

        async def execute(self, ctx, name, args):
            raise AssertionError("workspace read must be skipped for a trivial goal")

    orch.get_registry = lambda: _NoWorkspace()
    try:
        ctx = ToolContext(session_id=sid, project_id=1)
        out = asyncio.run(orch.orchestrate(ctx, [], session_id=sid))
    finally:
        orch.LLMClient = orig_client
        orch.run_turn = orig_run_turn
        orch.get_registry = orig_ws

    assert out == "single-loop answer"
    assert planner_calls["n"] == 0
