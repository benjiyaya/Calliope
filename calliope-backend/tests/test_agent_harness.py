"""Tests for the agent harness: tool registry, loop cycle, sessions, cancel,
blind-session auto-link, and the event-log derivations."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from calliope.agent.harness import log as session_log
from calliope.agent.harness import tools as harness_tools
from calliope.agent.harness.loop import MAX_ITERATIONS, _default_max_iterations, run_turn
from calliope.agent.harness.orchestrator import orchestrate
from calliope.agent.harness.registry import ToolContext
from calliope.agent.harness.tools import execute_tool, openai_tools_payload
from calliope.config import settings


# ─────────────────────────────────────────────────────────────────────────
# Tool registry
# ─────────────────────────────────────────────────────────────────────────


def test_tool_registry_schemas_valid():
    """Every registered tool exposes a valid JSON-schema object payload."""
    assert len(harness_tools.TOOLS) >= 18
    for name, t in harness_tools.TOOLS.items():
        assert t.name == name
        assert t.description and len(t.description) > 10, name
        assert t.parameters.get("type") == "object", name
        assert isinstance(t.parameters.get("properties"), dict), name
        for pname, pdef in t.parameters["properties"].items():
            assert isinstance(pdef, dict) and "type" in pdef, (name, pname)
        required = t.parameters.get("required", [])
        assert all(r in t.parameters["properties"] for r in required), name


def test_openai_payload_scoping():
    """Blind sessions see only requires_project=False tools.

    session_id is unused/high so a leftover local DB row cannot unlock
    requires_approval tools (those stay hidden until the user asks to render).
    """
    blind = ToolContext(session_id=9_999_001, project_id=None)
    linked = ToolContext(session_id=9_999_001, project_id=99)

    blind_names = {
        entry["function"]["name"] for entry in openai_tools_payload(blind)
    }
    linked_names = {
        entry["function"]["name"] for entry in openai_tools_payload(linked)
    }
    # Blind: workspace/bootstrap/system only
    assert "create_project" in blind_names
    assert "list_projects" in blind_names
    assert "get_workspace" in blind_names
    assert "comfy_server_info" in blind_names
    assert "run_workflow" not in blind_names  # HITL: hidden until user asks
    assert "attach_asset" in blind_names
    # Blind must NOT see project-scoped tools
    assert "generate_story" not in blind_names
    assert "enqueue_video_jobs" not in blind_names
    assert "list_scenes" not in blind_names
    # Linked: no bootstrap-only tools
    assert "create_project" not in linked_names
    assert "link_project" not in linked_names
    assert "generate_story" in linked_names
    # unlink_project is the way back: linked-only, hidden in sandbox
    assert "unlink_project" in linked_names
    assert "unlink_project" not in blind_names


def test_execute_tool_unknown_and_unscoped():
    blind = ToolContext(session_id=1, project_id=None)
    out = execute_tool_sync(blind, "nonexistent_tool", {})
    assert out["ok"] is False

    out = execute_tool_sync(blind, "list_scenes", {})
    assert out["ok"] is False
    assert "requires a linked project" in out["error"]


def execute_tool_sync(ctx: ToolContext, name: str, args: dict) -> dict:
    return asyncio.run(execute_tool(ctx, name, args))


# ─────────────────────────────────────────────────────────────────────────
# Loop: tool-call cycle with a fake LLM
# ─────────────────────────────────────────────────────────────────────────


class FakeStreamLLM:
    """Scripted streaming responses: first a tool call, then final text."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.calls: list[list[dict[str, Any]]] = []

    async def chat_stream(self, messages, temperature=0.4, tools=None, tool_choice=None):
        self.calls.append(messages)
        step = self.script.pop(0)
        if step["type"] == "tool_call":
            yield {"type": "tool_call", "tool_call": step["tool_call"]}
            yield {"type": "done"}
        else:
            for chunk in step["text"].split(" "):
                yield {"type": "delta", "content": chunk + " "}
            yield {"type": "done"}


@pytest.fixture
def fake_llm(monkeypatch):
    holder = {"client": None}

    def make(script: list[dict[str, Any]]) -> FakeStreamLLM:
        holder["client"] = FakeStreamLLM(script)
        return holder["client"]

    async def fake_client():
        return holder["client"]

    monkeypatch.setattr(
        "calliope.agent.harness.loop.LLMClient",
        lambda: _FakeClientWrapper(holder),
    )
    return make


class _FakeClientWrapper:
    def __init__(self, holder):
        self._holder = holder

    async def chat_stream(self, *a, **kw):
        client = self._holder["client"]
        assert client is not None, "script exhausted"
        async for ev in client.chat_stream(*a, **kw):
            yield ev

    async def close(self):
        return None


def test_loop_tool_call_cycle(fake_llm, client):
    """Loop: tool call → tool result message → final answer, all recorded."""
    # Create a real session so the loop can append log events.
    r = client.post("/api/agent/sessions", json={"title": "loop-test"})
    sid = r.json()["id"]
    executed: list[tuple[str, dict]] = []

    async def fake_execute(ctx, name, args):
        executed.append((name, args))
        return {"ok": True, "echo": args.get("value")}

    registry = harness_tools._registry
    orig = registry.execute
    registry.execute = fake_execute
    try:
        fake_llm(
            [
                {
                    "type": "tool_call",
                    "tool_call": {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_workspace", "arguments": "{}"},
                    },
                },
                {"type": "text", "text": "All done, workspace is ready."},
            ]
        )
        ctx = ToolContext(session_id=sid, project_id=None)
        history = [{"role": "user", "content": "check workspace"}]
        final = asyncio.run(run_turn(ctx, history))
    finally:
        registry.execute = orig

    assert final == "All done, workspace is ready."
    assert executed == [("get_workspace", {})]
    # History now contains: user, assistant+tool_calls, tool result, assistant
    roles = [m["role"] for m in history]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert history[1]["tool_calls"][0]["function"]["name"] == "get_workspace"
    assert json.loads(history[2]["content"])["ok"] is True


def test_loop_rejects_non_object_json_args(fake_llm, client):
    """A tool call with valid-JSON-but-not-an-object arguments must get a
    self-correcting error result, never reach a tool executor."""
    r = client.post("/api/agent/sessions", json={"title": "loop-badargs"})
    sid = r.json()["id"]
    executed: list[str] = []

    registry = harness_tools._registry
    orig = registry.execute
    registry.execute = lambda ctx, name, args: executed.append(name) or {"ok": True}
    try:
        fake_llm(
            [
                {
                    "type": "tool_call",
                    "tool_call": {
                        "id": "call_bad",
                        "type": "function",
                        "function": {"name": "list_scenes", "arguments": "[1,2,3]"},
                    },
                },
                {"type": "text", "text": "Recovered."},
            ]
        )
        ctx = ToolContext(session_id=sid, project_id=None)
        history = [{"role": "user", "content": "go"}]
        final = asyncio.run(run_turn(ctx, history))
    finally:
        registry.execute = orig

    assert final == "Recovered."
    assert executed == []  # executor never saw the malformed call
    result = json.loads(history[2]["content"])
    assert result["ok"] is False
    assert "must be a JSON object" in result["error"]

    # Event log: turn boundaries + tool call/result recorded.
    events = session_log.read_events(sid)
    types = [e.type for e in events]
    assert session_log.TURN_START in types
    assert session_log.TOOL_CALL in types
    assert session_log.TOOL_RESULT in types
    assert session_log.TURN_END in types


def test_loop_max_iterations(fake_llm, client):
    """Loop stops at the budget and reports it instead of spinning forever."""
    r = client.post("/api/agent/sessions", json={"title": "budget-test"})
    sid = r.json()["id"]

    async def fake_execute(ctx, name, args):
        return {"ok": True}

    registry = harness_tools._registry
    orig = registry.execute
    registry.execute = fake_execute
    try:
        fake_llm(
            [
                {
                    "type": "tool_call",
                    "tool_call": {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {"name": "get_workspace", "arguments": "{}"},
                    },
                }
                for i in range(50)
            ]
        )
        ctx = ToolContext(session_id=sid, project_id=42)
        history = [{"role": "user", "content": "loop forever"}]
        final = asyncio.run(run_turn(ctx, history, max_iterations=3))
    finally:
        registry.execute = orig

    assert "step budget" in final


def test_loop_default_max_iterations_follows_settings():
    """The step budget default is user-configurable via settings.agent_max_steps."""
    orig = settings.agent_max_steps
    try:
        settings.agent_max_steps = 40
        assert _default_max_iterations() == 40
        settings.agent_max_steps = 0  # clamped, never below 1
        assert _default_max_iterations() == 1
        settings.agent_max_steps = "bogus"  # type: ignore[assignment]
        assert _default_max_iterations() == MAX_ITERATIONS
    finally:
        settings.agent_max_steps = orig


# ─────────────────────────────────────────────────────────────────────────
# Orchestrator: planner + swarm fallbacks
# ─────────────────────────────────────────────────────────────────────────


def test_planner_llm_failure_degrades_to_single(fake_llm, client):
    """A planner LLM crash must not kill the turn — the single loop takes over."""
    r = client.post("/api/agent/sessions", json={"title": "plan-fail"})
    sid = r.json()["id"]

    import calliope.agent.harness.orchestrator as orch

    class _FailingPlanClient:
        async def chat(self, *a, **kw):
            raise ConnectionError("llm down")

        async def close(self):
            return None

    orig_client = orch.LLMClient
    orig_run_turn = orch.run_turn
    orch.LLMClient = lambda: _FailingPlanClient()
    captured: dict[str, Any] = {}

    async def fake_run_turn(ctx, history, *, on_message=None):
        captured["called"] = True
        return "single-loop fallback answer"

    orch.run_turn = fake_run_turn
    try:
        ctx = ToolContext(session_id=sid, project_id=None)
        out = asyncio.run(orchestrate(ctx, [], session_id=sid))
    finally:
        orch.LLMClient = orig_client
        orch.run_turn = orig_run_turn

    assert out == "single-loop fallback answer"
    assert captured.get("called") is True


def test_swarm_synthesis_failure_falls_back_to_reports(client):
    """When the synthesis LLM call fails after sub-agents ran, the user still
    gets a final answer assembled from the persisted sub-agent reports."""
    r = client.post("/api/agent/sessions", json={"title": "synth-fail"})
    sid = r.json()["id"]
    pid = _make_project(client)

    import calliope.agent.harness.orchestrator as orch

    class _SwarmThenFailClient:
        _n = 0  # class-level: a fresh instance per LLMClient() call

        async def chat(self, messages, temperature=0.2, **kw):
            _SwarmThenFailClient._n += 1
            if _SwarmThenFailClient._n == 1:  # planner: order a minimal swarm
                return json.dumps(
                    {
                        "mode": "swarm",
                        "note": "plan",
                        "tasks": [{"role": "script", "goal": "do thing"}],
                    }
                )
            raise ConnectionError("llm down")  # synthesis call

        async def close(self):
            return None

    async def fake_sub_agent(ctx, sub_history, allowed, *, agent_name, on_message=None, **kw):
        return "sub-agent did the thing"

    orig_client = orch.LLMClient
    orig_sub = orch._run_sub_agent
    orch.LLMClient = lambda: _SwarmThenFailClient()
    orch._run_sub_agent = fake_sub_agent
    try:
        ctx = ToolContext(session_id=sid, project_id=pid)
        out = asyncio.run(orchestrate(ctx, [], session_id=sid))
    finally:
        orch.LLMClient = orig_client
        orch._run_sub_agent = orig_sub

    # Fallback template carries the sub-agent report verbatim.
    assert "sub-agent did the thing" in out
    # And it was persisted as the final assistant message.
    rows = session_log.read_events(sid)
    final = [d for d in rows if d.type == session_log.ASSISTANT_MESSAGE and not d.data.get("agent_name")]
    assert any("sub-agent did the thing" in (d.data.get("content") or "") for d in final)
    # The swarm plan + per-task lifecycle were recorded and derive cleanly.
    plan = session_log.derive_plan(rows)
    assert plan is not None
    assert [t["role"] for t in plan["tasks"]] == ["script"]
    assert plan["tasks"][0]["status"] == "done"
    assert plan["note"] == "plan"


def test_turn_end_status_reflects_failure(client):
    """TURN_END carries the real outcome — a crashed turn must not be logged
    as 'completed' in the audit trail."""
    r = client.post("/api/agent/sessions", json={"title": "status-test"})
    sid = r.json()["id"]

    class _ExplodingStream:
        async def chat_stream(self, *a, **kw):
            raise ConnectionError("llm exploded")
            yield  # pragma: no cover — makes this an async generator

        async def close(self):
            return None

    import calliope.agent.harness.loop as loop_mod

    orig = loop_mod.LLMClient
    loop_mod.LLMClient = lambda: _ExplodingStream()
    try:
        with pytest.raises(ConnectionError):
            asyncio.run(run_turn(ToolContext(session_id=sid, project_id=None), []))
    finally:
        loop_mod.LLMClient = orig

    ends = [
        e for e in session_log.read_events(sid) if e.type == session_log.TURN_END
    ]
    assert len(ends) == 1
    assert ends[0].data["status"] == "failed"


def test_turn_end_status_completed_on_success(fake_llm, client):
    """A normal turn logs status 'completed'."""
    r = client.post("/api/agent/sessions", json={"title": "status-ok"})
    sid = r.json()["id"]
    fake_llm([{"type": "text", "text": "ok done"}])
    final = asyncio.run(
        run_turn(ToolContext(session_id=sid, project_id=None), [{"role": "user", "content": "hi"}])
    )
    assert final == "ok done"
    ends = [
        e for e in session_log.read_events(sid) if e.type == session_log.TURN_END
    ]
    assert ends[-1].data["status"] == "completed"


def test_turn_numbers_increment_across_turns(fake_llm, client):
    """max_turn_number drives numbering: consecutive turns get 1, 2, 3."""
    r = client.post("/api/agent/sessions", json={"title": "turns"})
    sid = r.json()["id"]
    for _ in range(3):
        fake_llm([{"type": "text", "text": "ok"}])
        asyncio.run(
            run_turn(
                ToolContext(session_id=sid, project_id=None),
                [{"role": "user", "content": "hi"}],
            )
        )
    starts = [
        e.data.get("turn")
        for e in session_log.read_events(sid)
        if e.type == session_log.TURN_START
    ]
    assert starts == [1, 2, 3]


# ─────────────────────────────────────────────────────────────────────────
# Router: sessions, blind auto-link, persistence, cancel
# ─────────────────────────────────────────────────────────────────────────


def _make_project(client) -> int:
    r = client.post("/api/projects", json={"title": "Test Film"})
    assert r.status_code == 200
    return r.json()["id"]


def test_agent_sessions_crud(client):
    r = client.post("/api/agent/sessions", json={"title": "My chat"})
    assert r.status_code == 200
    s = r.json()
    assert s["project_id"] is None
    assert s["status"] == "idle"

    r = client.get("/api/agent/sessions")
    assert [x["id"] for x in r.json()] == [s["id"]]

    r = client.get(f"/api/agent/sessions/{s['id']}")
    assert r.status_code == 200
    assert r.json()["messages"] == []

    r = client.delete(f"/api/agent/sessions/{s['id']}")
    assert r.json() == {"ok": True}

    r = client.get("/api/agent/sessions")
    assert r.json() == []


def test_agent_session_project_linkage(client):
    pid = _make_project(client)
    r = client.post("/api/agent/sessions", json={"project_id": pid})
    assert r.status_code == 200
    s = r.json()
    assert s["project_id"] == pid
    assert s["project"]["title"] == "Test Film"

    # Filter by project
    r = client.get(f"/api/agent/sessions?project_id={pid}")
    assert [x["id"] for x in r.json()] == [s["id"]]

    # Unlink → blind again
    r = client.patch(f"/api/agent/sessions/{s['id']}", json={"unlink": True})
    assert r.json()["project_id"] is None


def test_agent_message_persistence_and_status(client):
    """post_message persists user msg + turns session running; a failing run
    lands an error assistant message and sets status back."""
    from calliope.agent.harness import runner as runner_mod

    r = client.post("/api/agent/sessions", json={})
    sid = r.json()["id"]

    async def failing_orchestrate(ctx, history, *, session_id, on_message=None):
        raise RuntimeError("boom")

    orig = runner_mod.orchestrate
    runner_mod.orchestrate = failing_orchestrate
    try:
        # start_turn runs the agent task on the running loop; TestClient has one.
        r = client.post(f"/api/agent/sessions/{sid}/messages", json={"content": "hello"})
        assert r.status_code == 200
        # Give the background task a moment to fail and persist.
        import time

        deadline = time.time() + 5
        while time.time() < deadline:
            sess = client.get(f"/api/agent/sessions/{sid}").json()
            if sess["status"] == "error":
                break
            time.sleep(0.1)
        assert sess["status"] == "error"
        roles = [m["role"] for m in sess["messages"]]
        assert roles == ["user", "assistant"]
        assert "boom" in sess["messages"][-1]["content"]
        assert sess["title"] == "hello"  # auto-titled from first message
    finally:
        runner_mod.orchestrate = orig


def test_agent_cancel(client):
    """A cancelled run persists the interruption and returns to idle."""
    from calliope.agent.harness import runner as runner_mod

    r = client.post("/api/agent/sessions", json={})
    sid = r.json()["id"]

    async def slow_orchestrate(ctx, history, *, session_id, on_message=None):
        await asyncio.sleep(60)

    orig = runner_mod.orchestrate
    runner_mod.orchestrate = slow_orchestrate
    try:
        r = client.post(f"/api/agent/sessions/{sid}/messages", json={"content": "slow"})
        assert r.status_code == 200
        assert client.get(f"/api/agent/sessions/{sid}").json()["status"] == "running"

        r = client.post(f"/api/agent/sessions/{sid}/cancel")
        assert r.json()["ok"] is True

        import time

        deadline = time.time() + 5
        while time.time() < deadline:
            sess = client.get(f"/api/agent/sessions/{sid}").json()
            if sess["status"] == "idle":
                break
            time.sleep(0.1)
        assert sess["status"] == "idle"
        contents = [m["content"] for m in sess["messages"]]
        assert any("cancelled" in c for c in contents)
    finally:
        runner_mod.orchestrate = orig


def test_blind_session_create_project_autolinks(client):
    """The core sandbox flow: create_project in a blind session links it live."""
    r = client.post("/api/agent/sessions", json={})
    sid = r.json()["id"]

    ctx = ToolContext(session_id=sid, project_id=None)
    result = execute_tool_sync(
        ctx, "create_project", {"title": "From Sandbox", "idea": "agent-made"}
    )
    assert result["ok"] is True
    pid = result["project"]["id"]

    # Session linkage persisted in DB
    sess = client.get(f"/api/agent/sessions/{sid}").json()
    assert sess["project_id"] == pid
    assert sess["project"]["title"] == "From Sandbox"

    # Context flipped live: project-scoped tools now execute against pid
    assert ctx.project_id == pid
    result = execute_tool_sync(ctx, "list_scenes", {})
    assert result == {"ok": True, "result": []}


def test_linked_session_scoping(client):
    """A linked session's tools cannot read/write another project."""
    pid_a = _make_project(client)
    pid_b = _make_project(client)
    client.post(f"/api/projects/{pid_b}/scenes", json={"heading": "B scene", "order_index": 1})

    r = client.post("/api/agent/sessions", json={"project_id": pid_a})
    sid = r.json()["id"]

    ctx = ToolContext(session_id=sid, project_id=pid_a)
    scenes = execute_tool_sync(ctx, "list_scenes", {})
    assert scenes == {"ok": True, "result": []}  # B's scene invisible

    # Cross-project update rejected
    out = execute_tool_sync(ctx, "update_scene", {"scene_id": 1, "heading": "hack"})
    assert out["ok"] is False


def _add_scene(client, pid: int, heading: str, order: int) -> int:
    r = client.post(
        f"/api/projects/{pid}/scenes",
        json={"heading": heading, "order_index": order, "action": heading},
    )
    assert r.status_code == 200
    return r.json()["id"]


def test_list_scenes_search_and_order_labels(client):
    """#N is order; scene_id is the DB id. Search by query or clip numbers."""
    pid = _make_project(client)
    sid = client.post("/api/agent/sessions", json={"project_id": pid}).json()["id"]
    ctx = ToolContext(session_id=sid, project_id=pid)
    a = _add_scene(client, pid, "EXT. GANGNAM NIGHT", 1)
    b = _add_scene(client, pid, "INT. SAFEHOUSE", 2)
    _add_scene(client, pid, "EXT. SKYLINE", 3)

    all_rows = execute_tool_sync(ctx, "list_scenes", {})["result"]
    assert [s["order"] for s in all_rows] == [1, 2, 3]
    assert all_rows[0]["scene_id"] == a
    assert all_rows[0]["clip"] == "#1"
    assert all_rows[0]["id"] == a

    by_order = execute_tool_sync(ctx, "list_scenes", {"orders": [2]})["result"]
    assert [s["scene_id"] for s in by_order] == [b]
    assert by_order[0]["clip"] == "#2"

    found = execute_tool_sync(ctx, "list_scenes", {"query": "gangnam"})["result"]
    assert [s["order"] for s in found] == [1]


def test_update_scene_by_video_order(client):
    pid = _make_project(client)
    sid = client.post("/api/agent/sessions", json={"project_id": pid}).json()["id"]
    ctx = ToolContext(session_id=sid, project_id=pid)
    _add_scene(client, pid, "OLD HEADING", 1)
    out = execute_tool_sync(ctx, "update_scene", {"order": 1, "heading": "NEW HEADING"})
    assert out["ok"] is True
    assert out["scene"]["heading"] == "NEW HEADING"
    assert out["scene"]["order"] == 1
    assert out["scene"]["clip"] == "#1"


def test_enqueue_video_refuses_omit_and_bulk_dump(client):
    """A 'yes' after a 2-clip offer must not enqueue the whole timeline."""
    pid = _make_project(client)
    sid = client.post("/api/agent/sessions", json={"project_id": pid}).json()["id"]
    ctx = ToolContext(session_id=sid, project_id=pid)
    ids = [_add_scene(client, pid, f"S{i}", i) for i in range(1, 6)]
    session_log.append_event(sid, session_log.USER_MESSAGE, {"content": "yes"})

    omitted = execute_tool_sync(ctx, "enqueue_video_jobs", {"workflow_id": 40})
    assert omitted["ok"] is False
    assert "orders" in omitted["error"] or "scene_ids" in omitted["error"]

    dumped = execute_tool_sync(
        ctx, "enqueue_video_jobs", {"workflow_id": 40, "scene_ids": ids}
    )
    assert dumped["ok"] is False
    assert "bulk" in dumped["error"].lower()

    all_flag = execute_tool_sync(
        ctx, "enqueue_video_jobs", {"workflow_id": 40, "all_scenes": True}
    )
    assert all_flag["ok"] is False
    assert "bulk" in all_flag["error"].lower()


def test_workspace_digest_labels_order_and_id(client):
    from calliope.agent.harness.prompts import workspace_digest

    pid = _make_project(client)
    scene_id = _add_scene(client, pid, "EXT. TEST", 1)
    ctx = ToolContext(session_id=1, project_id=pid)
    digest = workspace_digest(ctx)
    assert f"#{scene_id}:" not in digest
    assert f"#1 id={scene_id}:" in digest


def test_bulk_video_policy_phrases():
    from calliope.agent.harness.policy import allows_bulk_video_enqueue

    assert allows_bulk_video_enqueue("yes", 2) is True
    assert allows_bulk_video_enqueue("yes", 24) is False
    assert allows_bulk_video_enqueue("generate all scenes", 24) is True
    assert allows_bulk_video_enqueue("do every clip", 10) is True


def test_sub_agent_failure_names_empty_str_exception(client):
    """An exception whose str() is EMPTY must still be identifiable in the
    failure message and event log. Observed live 2026-08-25: 'Sub-agent
    failed: ' with nothing after the colon — an httpx.ReadError('') raised
    when a deploy restart killed the in-flight stream."""
    import httpx
    import calliope.agent.harness.orchestrator as orch
    from calliope.agent.harness import log as session_log

    r = client.post("/api/agent/sessions", json={"title": "crash-detail"})
    sid = r.json()["id"]
    pid = _make_project(client)

    class _SwarmPlanClient:
        async def chat(self, messages, temperature=0.2, **kw):
            return json.dumps(
                {
                    "mode": "swarm",
                    "note": "plan",
                    "tasks": [{"role": "assets", "goal": "update text assets"}],
                }
            )

        async def close(self):
            return None

    async def dying_sub_agent(ctx, sub_history, allowed, *, agent_name, on_message=None, **kw):
        raise httpx.ReadError("")

    orig_client = orch.LLMClient
    orig_sub = orch._run_sub_agent
    orch.LLMClient = lambda: _SwarmPlanClient()
    orch._run_sub_agent = dying_sub_agent
    try:
        ctx = ToolContext(session_id=sid, project_id=pid)
        asyncio.run(orchestrate(ctx, [], session_id=sid))
    finally:
        orch.LLMClient = orig_client
        orch._run_sub_agent = orig_sub

    events = session_log.read_events(sid)
    fails = [
        e.data for e in events
        if e.type == session_log.ASSISTANT_MESSAGE
        and str(e.data.get("content", "")).startswith("Sub-agent failed:")
    ]
    assert fails, "no failure message recorded"
    assert "ReadError" in fails[0]["content"]
