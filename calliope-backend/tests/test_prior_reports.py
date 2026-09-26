"""Prior task reports + memory/skills in the swarm.

Finding 3 (stateless sub-agents) + the user catch: sub-agents get (a) a
capped digest of the last prior task reports in their kickoff message and
(b) the memory + skills tools and prompt sections the main loop has.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from calliope.agent.harness.registry import ToolContext
from calliope.config import settings
from calliope.db import get_db

from calliope.agent.harness.orchestrator import (
    _PRIOR_REPORT_CHARS,
    _prior_reports_digest,
    ROLE_TOOLS,
)

MEMORY_SKILL_TOOLS = {
    "save_memory",
    "list_memories",
    "forget_memory",
    "read_skill",
    "list_skills",
}


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
            ("prior-reports-test",),
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
    def __init__(self, responses):
        self._responses = list(responses)
        self.seen_messages: list[list[dict]] = []

    async def chat_with_tools(self, messages, temperature=0.7, tools=None, tool_choice=None):
        self.seen_messages.append(list(messages))
        if self._responses:
            return self._responses.pop(0)
        return {"role": "assistant", "content": "done", "tool_calls": []}

    async def close(self):
        pass


class _StubRegistry:
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
    orch.LLMClient = lambda: client
    orch.get_registry = lambda: registry


# ── _prior_reports_digest unit behavior ─────────────────────────────────


def test_digest_empty_when_no_results():
    assert _prior_reports_digest([]) == ""


def test_digest_keeps_last_two_and_caps_chars():
    reports = [f"[story] report {i} " + "x" * 40 for i in range(4)]
    digest = _prior_reports_digest(reports)
    assert "[story] report 0" not in digest
    assert "[story] report 1" not in digest
    assert "[story] report 2" in digest and "[story] report 3" in digest
    assert digest.startswith("Prior task reports:")


def test_digest_truncates_long_reports():
    long = "[story] " + "y" * (_PRIOR_REPORT_CHARS + 500)
    digest = _prior_reports_digest([long])
    assert "…[truncated]" in digest
    assert len(digest) < _PRIOR_REPORT_CHARS + 200


def test_digest_marks_failed_reports():
    digest = _prior_reports_digest(["[script] FAILED: timeout"])
    assert "FAILED: timeout" in digest


# ── swarm integration: digest lands in the sub-agent's kickoff message ──


def test_swarm_second_task_sees_first_task_report():
    import calliope.agent.harness.orchestrator as orch

    sid = _mk_session()
    client = _ScriptedClient(
        [
            {"role": "assistant", "content": "story work finished", "tool_calls": []},
            {"role": "assistant", "content": "script work finished", "tool_calls": []},
        ]
    )
    registry = _StubRegistry()
    orig_client, orig_reg = orch.LLMClient, orch.get_registry
    _install(orch, client, registry)

    ctx = ToolContext(session_id=sid, project_id=1)
    tasks = [
        {"role": "story", "goal": "write beats"},
        {"role": "script", "goal": "write scenes"},
    ]
    try:
        # Directly exercise the loop body the way orchestrate does:
        results: list[str] = []
        from calliope.agent.harness.orchestrator import _prior_reports_digest

        for task in tasks:
            digest = _prior_reports_digest(results)
            sub_history = [
                {
                    "role": "user",
                    "content": (
                        f"You are the {task['role']} sub-agent. Goal: {task['goal']}\n"
                        f"Project: #{ctx.project_id}.\n"
                        + (digest + "\n" if digest else "")
                        + "Complete your goal."
                    ),
                }
            ]
            answer = asyncio.run(
                orch._run_sub_agent(
                    ctx, sub_history, ROLE_TOOLS[task["role"]], agent_name=f"{task['role']}-agent"
                )
            )
            results.append(f"[{task['role']}] {answer}")
    finally:
        orch.LLMClient, orch.get_registry = orig_client, orig_reg

    # seen_messages[1] is the second task's [system, user...] batch; the user
    # kickoff message carries the digest.
    second_user = next(
        m for m in client.seen_messages[1] if m.get("role") == "user"
    )
    assert "Prior task reports:" in second_user["content"]
    assert "[story] story work finished" in second_user["content"]


# ── memory + skills tools present in every role ──────────────────────────


@pytest.mark.parametrize("role", ["story", "script", "assets", "video"])
def test_memory_skill_tools_in_every_role(role):
    for tool in MEMORY_SKILL_TOOLS:
        assert tool in ROLE_TOOLS[role], f"{tool} missing from {role}"


# ── memory + skills sections render into the sub-agent system prompt ────


def test_sub_agent_system_prompt_includes_memory_and_skills_sections():
    import calliope.agent.harness.orchestrator as orch

    sid = _mk_session()
    client = _ScriptedClient([{"role": "assistant", "content": "ok", "tool_calls": []}])
    registry = _StubRegistry()
    orig_client, orig_reg = orch.LLMClient, orch.get_registry
    _install(orch, client, registry)
    try:
        ctx = ToolContext(session_id=sid, project_id=None)
        asyncio.run(
            orch._run_sub_agent(
                ctx, [{"role": "user", "content": "go"}], ["get_workspace"], agent_name="story-agent"
            )
        )
    finally:
        orch.LLMClient, orch.get_registry = orig_client, orig_reg

    system = client.seen_messages[0][0]["content"]
    assert "## Memory" in system or "## Skills" in system, (
        "sub-agent system prompt renders neither memory nor skills section"
    )
