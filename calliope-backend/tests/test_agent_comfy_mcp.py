"""Tests for the comfy-mcp agent plugin.

All tests use a mocked MCP client — no live ComfyUI or comfy-cli required.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from anyio import BrokenResourceError, ClosedResourceError

from calliope.agent.harness.plugins import comfy_mcp
from calliope.agent.harness.plugins.comfy_mcp import ComfyMcpClient
from calliope.agent.harness.prompts import SystemPromptService
from calliope.agent.harness.registry import ToolContext, ToolRegistry


# ── Helpers ─────────────────────────────────────────────────────────────────


def _fresh_registry() -> tuple[ToolRegistry, SystemPromptService]:
    """Build a registry with only the comfy-mcp plugin loaded."""
    registry = ToolRegistry()
    prompts = SystemPromptService()
    comfy_mcp.register(registry, prompts)
    return registry, prompts


def _make_mock_client(session: AsyncMock) -> ComfyMcpClient:
    """Build a ComfyMcpClient with a pre-set session (skipping spawn)."""
    client = ComfyMcpClient()
    client._session = session
    client._ctx = MagicMock()
    client._ctx.__aenter__ = AsyncMock(return_value=(AsyncMock(), AsyncMock()))
    client._ctx.__aexit__ = AsyncMock(return_value=False)
    return client


# ── Tests: tool registration ────────────────────────────────────────────────


def test_register_populates_tools():
    """The plugin registers a meaningful set of bridge tools."""
    registry, _ = _fresh_registry()
    names = set(registry.tools.keys())
    for expected in [
        "comfy_server_info",
        "comfy_search_nodes",
        "comfy_search_models",
        "comfy_validate_workflow",
        "comfy_run_workflow",
        "comfy_launch_comfyui",
        "comfy_stop_comfyui",
        "comfy_job",
        "comfy_fetch_outputs",
        "comfy_install_node",
    ]:
        assert expected in names, f"{expected} not registered"
    assert len(names) >= 25, f"Expected >=25 tools, got {len(names)}"


def test_register_populates_prompt_section():
    """The plugin registers a prompt section."""
    _, prompts = _fresh_registry()
    keys = [s.key for s in prompts._sections]
    assert "comfy_mcp" in keys


def test_no_skipped_tools():
    """Tools in the _SKIP_TOOLS set must not appear."""
    registry, _ = _fresh_registry()
    names = set(registry.tools.keys())
    assert "comfy_submit_feedback" not in names
    assert "comfy_report_session_summary" not in names


# ── Tests: schema validation ───────────────────────────────────────────────


def test_all_schemas_are_valid_json_schema():
    """Every registered tool has a valid JSON-schema object payload."""
    registry, _ = _fresh_registry()
    for name, t in registry.tools.items():
        assert t.name == name, f"Name mismatch: {t.name} != {name}"
        assert t.description and len(t.description) > 5, f"Short description: {name}"
        assert t.parameters.get("type") == "object", f"Not object schema: {name}"
        assert isinstance(t.parameters.get("properties"), dict), f"No properties: {name}"
        for req in t.parameters.get("required", []):
            assert req in t.parameters["properties"], (
                f"Required key {req} missing from properties: {name}"
            )


def test_destructive_flags():
    """Destructive tools are flagged; read-only tools are not."""
    registry, _ = _fresh_registry()
    assert registry.tools["comfy_server_info"].destructive is False
    assert registry.tools["comfy_search_nodes"].destructive is False
    assert registry.tools["comfy_job"].destructive is False
    assert registry.tools["comfy_run_workflow"].destructive is True
    assert registry.tools["comfy_stop_comfyui"].destructive is True
    assert registry.tools["comfy_install_node"].destructive is True
    assert registry.tools["comfy_launch_comfyui"].destructive is True


def test_requires_project_flags():
    """All comfy-mcp tools work without a linked project (requires_project=False)."""
    registry, _ = _fresh_registry()
    for name, t in registry.tools.items():
        assert t.requires_project is False, f"{name} requires_project=True"


# ── Tests: executor bridging with mocked MCP ────────────────────────────────


def test_executor_bridges_to_mcp():
    """An executor calls the matching MCP tool and returns the result."""
    session = AsyncMock()
    text_block = MagicMock()
    text_block.text = '{"ok": true, "running": true, "url": "http://127.0.0.1:8188"}'
    call_result = MagicMock()
    call_result.content = [text_block]
    session.call_tool = AsyncMock(return_value=call_result)

    client = _make_mock_client(session)
    with patch("calliope.agent.harness.plugins.comfy_mcp._get_client", return_value=client):
        executor = comfy_mcp._make_executor("server_info")
        ctx = ToolContext(session_id=1, project_id=None)
        result = asyncio.run(executor(ctx, {}))

    assert result["ok"] is True
    assert result["running"] is True
    session.call_tool.assert_awaited_once_with("server_info", {})


def test_executor_passes_arguments():
    """Arguments are forwarded verbatim to the MCP tool."""
    session = AsyncMock()
    text_block = MagicMock()
    text_block.text = '{"ok": true}'
    call_result = MagicMock()
    call_result.content = [text_block]
    session.call_tool = AsyncMock(return_value=call_result)

    client = _make_mock_client(session)
    with patch("calliope.agent.harness.plugins.comfy_mcp._get_client", return_value=client):
        executor = comfy_mcp._make_executor("run_workflow")
        ctx = ToolContext(session_id=1, project_id=42)
        asyncio.run(executor(ctx, {"workflow_path": "/tmp/test.json", "wait": False}))

    session.call_tool.assert_awaited_once_with(
        "run_workflow", {"workflow_path": "/tmp/test.json", "wait": False}
    )


def test_executor_returns_error_dict_on_failure():
    """When MCP is unreachable, the executor returns an error dict — not an exception."""
    session = AsyncMock()
    session.call_tool = AsyncMock(side_effect=ConnectionError("spawn failed"))

    client = _make_mock_client(session)
    client._session = None
    client._connect = AsyncMock(side_effect=ConnectionError("spawn failed"))

    with patch("calliope.agent.harness.plugins.comfy_mcp._get_client", return_value=client):
        executor = comfy_mcp._make_executor("server_info")
        ctx = ToolContext(session_id=1, project_id=None)
        result = asyncio.run(executor(ctx, {}))

    assert result["ok"] is False
    assert "ConnectionError" in result["error"]
    assert "spawn failed" in result["error"]


def test_executor_handles_mcp_text_content():
    """MCP results with plain text (non-JSON) are returned as-is."""
    session = AsyncMock()
    text_block = MagicMock()
    text_block.text = "ComfyUI stopped."
    call_result = MagicMock()
    call_result.content = [text_block]
    session.call_tool = AsyncMock(return_value=call_result)

    client = _make_mock_client(session)
    with patch("calliope.agent.harness.plugins.comfy_mcp._get_client", return_value=client):
        executor = comfy_mcp._make_executor("stop_comfyui")
        ctx = ToolContext(session_id=1, project_id=None)
        result = asyncio.run(executor(ctx, {}))

    assert result == "ComfyUI stopped."


def test_executor_handles_multiple_content_blocks():
    """Multiple content blocks are returned as a list."""
    session = AsyncMock()
    block1 = MagicMock()
    block1.text = '{"status": "done"}'
    block2 = MagicMock()
    block2.text = "Additional info"
    call_result = MagicMock()
    call_result.content = [block1, block2]
    session.call_tool = AsyncMock(return_value=call_result)

    client = _make_mock_client(session)
    with patch("calliope.agent.harness.plugins.comfy_mcp._get_client", return_value=client):
        executor = comfy_mcp._make_executor("job")
        ctx = ToolContext(session_id=1, project_id=None)
        result = asyncio.run(executor(ctx, {"action": "status", "prompt_id": "abc"}))

    assert isinstance(result, list)
    assert len(result) == 2


# ── Tests: reconnect behavior ────────────────────────────────────────────────


def test_call_tool_retries_once_on_write_pipe_death():
    """A write-side pipe failure is retried once on a fresh session."""
    client = ComfyMcpClient()
    good_session = AsyncMock()

    def make_block(payload: Any):
        block = MagicMock()
        block.text = payload if isinstance(payload, str) else json.dumps(payload)
        result = MagicMock()
        result.content = [block]
        return result

    good_session.call_tool = AsyncMock(return_value=make_block({"ok": True}))

    dead_session = AsyncMock()
    dead_session.call_tool = AsyncMock(side_effect=ClosedResourceError("write pipe dead"))

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=(AsyncMock(), AsyncMock()))
    ctx.__aexit__ = AsyncMock(return_value=False)

    connect_attempts: list[Any] = []

    async def fake_connect():
        connect_attempts.append(1)
        if len(connect_attempts) == 1:
            client._session = dead_session
            client._ctx = ctx
        else:
            client._session = good_session
            client._ctx = ctx

    client._connect = AsyncMock(side_effect=fake_connect)
    client._ensure_connected = ComfyMcpClient._ensure_connected.__get__(client)

    out = asyncio.run(client.call_tool("server_info", {}))
    assert out["ok"] is True
    assert len(connect_attempts) == 2  # first died, second succeeded
    # The dead session's exit was invoked during teardown.
    ctx.__aexit__.assert_awaited()


def test_call_tool_does_not_retry_app_level_errors():
    """A tool that raises a plain exception must NOT be retried inline
    (it may have already executed server-side), but the session is marked
    dead so the next call reconnects."""
    client = ComfyMcpClient()
    session = AsyncMock()
    session.call_tool = AsyncMock(side_effect=RuntimeError("app-level failure"))

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=(AsyncMock(), AsyncMock()))
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.__aexit__ = AsyncMock(return_value=False)

    async def fake_connect():
        client._session = session
        client._session.__aexit__ = AsyncMock(return_value=False)
        client._ctx = ctx

    client._connect = AsyncMock(side_effect=fake_connect)
    client._ensure_connected = ComfyMcpClient._ensure_connected.__get__(client)

    with pytest.raises(RuntimeError):
        asyncio.run(client.call_tool("run_workflow", {}))
    assert session.call_tool.await_count == 1  # no inline retry
    assert client._session is None  # next call will reconnect fresh
    ctx.__aexit__.assert_awaited()  # subprocess cleaned up


def test_call_tool_retries_at_most_once():
    """Two pipe deaths in a row → the second raises (no infinite loop)."""
    client = ComfyMcpClient()

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=(AsyncMock(), AsyncMock()))
    ctx.__aexit__ = AsyncMock(return_value=False)

    calls = {"n": 0}

    async def fake_connect():
        calls["n"] += 1
        session = AsyncMock()
        session.call_tool = AsyncMock(side_effect=BrokenResourceError("dead again"))
        client._session = session
        client._ctx = ctx

    client._connect = AsyncMock(side_effect=fake_connect)
    client._ensure_connected = ComfyMcpClient._ensure_connected.__get__(client)

    with pytest.raises(BrokenResourceError):
        asyncio.run(client.call_tool("server_info", {}))
    # Attempt 1 dies (retried once), attempt 2 also dies → raise, no attempt 3.
    assert calls["n"] == 2


def test_close_clean_state_after_teardown():
    """close() clears session/ctx/cache so a subsequent call reconnects."""
    client = ComfyMcpClient()
    session = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=(AsyncMock(), AsyncMock()))
    ctx.__aexit__ = AsyncMock(return_value=False)
    client._session = session
    client._ctx = ctx
    client._tools_cache = [{"name": "server_info"}]

    asyncio.run(client.close())
    assert client._session is None
    assert client._ctx is None
    assert client._tools_cache is None
    session.__aexit__.assert_awaited_once()
    ctx.__aexit__.assert_awaited_once()


def test_build_harness_omits_mcp_bridge():
    """Composed harness keeps HTTP comfy_server_info and drops MCP comfy_* tools."""
    from calliope.agent.harness import tools as harness_tools

    names = set(harness_tools.TOOLS.keys())
    assert "comfy_server_info" in names
    mcp_names = [
        n
        for n in names
        if n.startswith("comfy_") and n != "comfy_server_info"
    ]
    assert mcp_names == [], mcp_names
    assert "run_workflow" in names


# ── Tests: through the full harness (via tools.py compat shim) ──────────────


def test_comfy_mcp_tools_not_in_full_harness():
    """MCP bridge tools must not appear on the live tool payload."""
    from calliope.agent.harness import tools as harness_tools

    names = set(harness_tools.TOOLS.keys())
    assert "comfy_run_workflow" not in names
    assert "comfy_search_templates" not in names
    assert "comfy_run_template" not in names


def test_comfy_server_info_scoping():
    """Native health check stays visible in both blind and linked sessions."""
    from calliope.agent.harness import tools as harness_tools

    blind = ToolContext(session_id=1, project_id=None)
    linked = ToolContext(session_id=1, project_id=99)

    blind_names = {e["function"]["name"] for e in harness_tools.openai_tools_payload(blind)}
    linked_names = {e["function"]["name"] for e in harness_tools.openai_tools_payload(linked)}

    assert "comfy_server_info" in blind_names
    assert "comfy_server_info" in linked_names
    assert "comfy_run_workflow" not in blind_names
    assert "comfy_run_workflow" not in linked_names
