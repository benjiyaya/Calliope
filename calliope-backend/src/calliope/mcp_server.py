"""MCP server: drive Calliope from Claude Code (or any MCP client).

Mounted on the backend at ``/mcp`` (streamable HTTP) instead of running as a
separate process: tools execute INSIDE the backend, so every change goes out
on the same event bus the web app listens to and the UI updates live — the
same as when Calliope's own agent works.

The tools are the agent harness registry's (same executors, same scoping),
minus the ones that only make sense inside Calliope's chat. Project binding
lives on a dedicated agent session ("Claude Code (MCP)"): ``select_project``
sets it, and ``create_project`` links the new project to it on its own.

Approval: the harness policy reads intent from the chat's event log ("the
user's command is the permission"). Over MCP there is no chat — the human in
the loop is the MCP client's per-tool permission prompt — so ToolContext
origin ``mcp`` is honoured by the policy (see policy.user_allows_render and
harness._user_confirmed_replacement), and rendering / destructive tools are
flagged with ``destructiveHint`` so clients ask before running them.

Claude Code:  claude mcp add --transport http calliope http://127.0.0.1:8247/mcp
"""
from __future__ import annotations

import json
import logging
from typing import Any

import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from calliope import __version__
from calliope.agent.harness import get_registry
from calliope.agent.harness.registry import ToolContext, ToolDefinition
from calliope.config import settings
from calliope.db import get_db, row_to_dict
from calliope.events.bus import event_bus

logger = logging.getLogger("calliope.mcp")

MCP_ORIGIN = "mcp"
MCP_SESSION_TITLE = "Claude Code (MCP)"
MCP_AGENT_NAME = "claude-code"

# Chat-only or surface-bound tools. Build Scene (shot) tools act on a
# per-session 3D composition, canvas tools on the AI Canvas board, ask_user
# needs Calliope's chat to answer, run_command duplicates the MCP client's own
# shell, and link/unlink are replaced by select_project.
_EXCLUDED_CATEGORIES = frozenset({"shot", "canvas", "interaction"})
_EXCLUDED_TOOLS = frozenset({"run_command", "link_project", "unlink_project"})
_READ_ONLY_PREFIXES = ("get_", "list_", "read_", "summarize_")
_READ_ONLY_TOOLS = frozenset({"comfy_server_info", "wait_for_jobs"})

INSTRUCTIONS = """Calliope turns an idea into a film: project → story (beats,
characters, locations, items) → script (scenes, clips) → reference images →
video clips rendered by ComfyUI. Every change shows up live in the Calliope web
app (http://127.0.0.1:5173).

Start with list_projects, then select_project (or create_project, which selects
the new project). get_workspace summarizes the selected project. Read the
matching skill first when unsure (list_skills / read_skill — e.g.
scene-to-video). Rendering (enqueue_asset_jobs, enqueue_video_jobs,
run_workflow) queues real GPU work one job at a time; poll with
get_job_status / wait_for_jobs. generate_story / generate_script with
replace=true DELETE existing content.

YOU write the content (Settings → Agent → MCP content source = client, the
default): Calliope's own LLM is not used. generate_story, generate_script,
break_into_shots and set_continuity_plan called WITHOUT content/plan return a
brief — the exact instructions and context — then call them again WITH
content/plan to save it (same validation as Calliope's drafts; nothing is
deleted until the content passes). Video: get_prompt_brief → write each H3
prompt → set_clip_prompts → enqueue_video_jobs (clips without a current
prompt are refused). Pipeline: story → script (scenes may carry their clips)
→ set_continuity_plan → reference images → prompts → render. If the setting
is "calliope", the same tools generate with Calliope's LLM instead."""

SELECT_PROJECT_TOOL = types.Tool(
    name="select_project",
    description=(
        "Select the Calliope project the other tools act on (story, script, "
        "assets, video). Returns the project's workspace summary. Pass "
        "project_id=null to clear the selection."
    ),
    input_schema={
        "type": "object",
        "properties": {"project_id": {"type": ["integer", "null"]}},
        "required": ["project_id"],
    },
    annotations=types.ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
)


# ── session: the project binding the tools run against ─────────────────


def _mcp_session() -> dict[str, Any]:
    """The dedicated agent session, created on first use.

    agent_sessions.origin only allows 'chat'/'scene', so the row is a chat
    session identified by its title; the ToolContext carries origin='mcp'.
    """
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT * FROM agent_sessions WHERE title = ? AND origin = 'chat' ORDER BY id LIMIT 1",
            (MCP_SESSION_TITLE,),
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO agent_sessions (title, status, origin) VALUES (?, 'idle', 'chat')",
                (MCP_SESSION_TITLE,),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM agent_sessions WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return row_to_dict(row)
    finally:
        conn.close()


def _set_project(session_id: int, project_id: int | None) -> dict[str, Any] | None:
    conn = get_db(settings.db_path)
    try:
        project = None
        if project_id is not None:
            project = conn.execute(
                "SELECT id, title FROM projects WHERE id = ? AND status != 'system'",
                (project_id,),
            ).fetchone()
            if project is None:
                return None
        conn.execute(
            "UPDATE agent_sessions SET project_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (project_id, session_id),
        )
        conn.commit()
        return row_to_dict(project) if project else {}
    finally:
        conn.close()


# ── tool mapping ────────────────────────────────────────────────────────


def _exposed(t: ToolDefinition) -> bool:
    return t.category not in _EXCLUDED_CATEGORIES and t.name not in _EXCLUDED_TOOLS


def _to_mcp_tool(t: ToolDefinition) -> types.Tool:
    read_only = t.name.startswith(_READ_ONLY_PREFIXES) or t.name in _READ_ONLY_TOOLS
    description = t.description
    if t.requires_approval:
        description += " [Starts GPU rendering in ComfyUI.]"
    if t.requires_project:
        description += " [Acts on the project chosen with select_project.]"
    return types.Tool(
        name=t.name,
        description=description,
        input_schema=t.parameters or {"type": "object", "properties": {}},
        annotations=types.ToolAnnotations(
            read_only_hint=read_only,
            # Rendering spends GPU time and replaces clip renders; deletes and
            # replace=true regenerations remove content.
            destructive_hint=not read_only and (t.destructive or t.requires_approval),
            open_world_hint=False,
        ),
    )


def _result(payload: Any, *, error: bool = False) -> types.CallToolResult:
    text = json.dumps(payload, ensure_ascii=False, default=str, indent=1)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], is_error=error
    )


async def _list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
    registry = get_registry()
    tools = [SELECT_PROJECT_TOOL] + [
        _to_mcp_tool(t) for t in registry.tools.values() if _exposed(t)
    ]
    return types.ListToolsResult(tools=tools)


async def call_tool(name: str, args: dict[str, Any]) -> types.CallToolResult:
    """Execute one MCP tool call (module-level so tests can drive it directly)."""
    session = _mcp_session()
    session_id = int(session["id"])
    if name == SELECT_PROJECT_TOOL.name:
        project_id = args.get("project_id")
        selected = _set_project(session_id, int(project_id) if project_id is not None else None)
        if selected is None:
            return _result({"ok": False, "error": f"No project with id {project_id}"}, error=True)
        if project_id is None:
            return _result({"ok": True, "selected_project": None})
        name, args = "get_workspace", {}
        session = _mcp_session()

    registry = get_registry()
    t = registry.get(name)
    if t is None or not _exposed(t):
        return _result({"ok": False, "error": f"Unknown tool: {name}"}, error=True)
    project_id = session.get("project_id")
    ctx = ToolContext(
        session_id=session_id,
        # Blind-only tools (create_project, list_projects) refuse a linked
        # context; they don't depend on the selection anyway.
        project_id=None if t.blind_only else project_id,
        origin=MCP_ORIGIN,
    )
    await event_bus.publish(
        "agent.tool",
        {"session_id": session_id, "agent_name": MCP_AGENT_NAME, "phase": "start", "tool": name, "args": args},
    )
    result = await registry.execute(ctx, name, args)
    await event_bus.publish(
        "agent.tool",
        {"session_id": session_id, "agent_name": MCP_AGENT_NAME, "phase": "finish", "tool": name, "result": result},
    )
    failed = isinstance(result, dict) and result.get("ok") is False
    return _result(result, error=failed)


async def _call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
    try:
        return await call_tool(params.name, dict(params.arguments or {}))
    except Exception as exc:  # noqa: BLE001 — surfaced to the client, never a transport error
        logger.exception("MCP tool %s failed", params.name)
        detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        return _result({"ok": False, "error": detail}, error=True)


# ── server + HTTP transport ─────────────────────────────────────────────


def build_session_manager() -> StreamableHTTPSessionManager:
    """One manager per app instance (its run() context is single-use)."""
    server = Server(
        "calliope",
        version=__version__,
        title="Calliope",
        instructions=INSTRUCTIONS,
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
    )
    hosts = ["127.0.0.1", "localhost"]
    return StreamableHTTPSessionManager(
        server,
        # Stateless + JSON: no server-side sessions to lose when the dev
        # backend reloads, and every client transport can read the replies.
        stateless=True,
        json_response=True,
        # The backend is local-only; refuse DNS-rebinding requests from web
        # pages (the REST API's CORS "*" must not extend to tool execution).
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"{h}:*" for h in hosts] + hosts,
            allowed_origins=[f"http://{h}:*" for h in hosts],
        ),
    )


class MCPEndpoint:
    """ASGI endpoint for the /mcp route (a class, so Starlette mounts it raw)."""

    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self.manager = manager

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        await self.manager.handle_request(scope, receive, send)
