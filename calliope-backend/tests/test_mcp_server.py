"""MCP server mounted at /mcp: tool exposure, project binding, approvals, transport."""
from __future__ import annotations

import asyncio
import json

from calliope.agent.harness import _render_approval_guard, get_registry
from calliope.agent.harness.registry import ToolContext
from calliope import mcp_server

MCP_HEADERS = {
    "Host": "127.0.0.1:8247",
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _rpc(client, method: str, params: dict | None = None, headers: dict | None = None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post("/mcp", json=body, headers=headers or MCP_HEADERS)


def _call(name: str, args: dict | None = None) -> tuple[dict, bool]:
    result = asyncio.run(mcp_server.call_tool(name, args or {}))
    return json.loads(result.content[0].text), bool(result.is_error)


def _tools() -> dict:
    listed = asyncio.run(mcp_server._list_tools(None, None))
    return {t.name: t for t in listed.tools}


def test_http_endpoint_initializes_and_lists_tools(client):
    init = _rpc(
        client,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    )
    assert init.status_code == 200, init.text
    assert init.json()["result"]["serverInfo"]["name"] == "calliope"

    listed = _rpc(client, "tools/list")
    assert listed.status_code == 200, listed.text
    names = {t["name"] for t in listed.json()["result"]["tools"]}
    assert {"select_project", "list_projects", "generate_script", "enqueue_video_jobs"} <= names


def test_http_endpoint_rejects_foreign_host(client):
    """DNS-rebinding protection: a web page must not reach the tools."""
    r = _rpc(client, "tools/list", headers={**MCP_HEADERS, "Host": "evil.example"})
    assert r.status_code in (403, 421), r.status_code


def test_exposed_tools_skip_chat_only_surfaces(client):
    tools = _tools()
    for hidden in ("ask_user", "run_command", "link_project", "unlink_project", "add_object"):
        assert hidden not in tools
    assert not any(n.startswith("canvas_") for n in tools)
    assert tools["list_projects"].annotations.read_only_hint is True
    assert tools["delete_scene"].annotations.destructive_hint is True
    # Rendering spends GPU time → the client should ask before running it.
    assert tools["enqueue_video_jobs"].annotations.destructive_hint is True
    assert "GPU" in tools["enqueue_video_jobs"].description


def test_create_project_selects_it_and_project_tools_follow(client):
    created, err = _call("create_project", {"title": "MCP Film", "idea": "a lighthouse keeper"})
    assert not err, created
    pid = created.get("project", created).get("id") or created.get("project_id")
    assert pid
    assert mcp_server._mcp_session()["project_id"] == pid

    ws, err = _call("get_workspace")
    assert not err and json.dumps(ws).count("MCP Film")

    added, err = _call("add_character", {"name": "Ada", "appearance": "grey coat"})
    assert not err, added
    story = client.get(f"/api/projects/{pid}/story").json()
    assert [c["name"] for c in story["characters"]] == ["Ada"]


def test_select_project_validates_and_clears(client):
    out, err = _call("select_project", {"project_id": 99999})
    assert err and "No project" in out["error"]
    out, err = _call("select_project", {"project_id": None})
    assert not err and out["selected_project"] is None
    out, err = _call("add_character", {"name": "Nobody"})
    assert err  # requires a selected project


def test_mcp_origin_counts_as_render_approval(client):
    """No chat to read intent from: the client's permission prompt approves."""
    registry = get_registry()
    tool = registry.get("enqueue_video_jobs")
    session_id = int(mcp_server._mcp_session()["id"])
    mcp_ctx = ToolContext(session_id=session_id, project_id=1, origin="mcp")
    chat_ctx = ToolContext(session_id=session_id, project_id=1, origin="chat")
    assert _render_approval_guard(mcp_ctx, tool, {}).kind == "allow"
    assert _render_approval_guard(chat_ctx, tool, {}).kind == "deny"
