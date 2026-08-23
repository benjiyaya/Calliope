"""ComfyUI MCP plugin: direct-to-ComfyUI tooling via the comfy-mcp stdio server.

Not loaded by the composed harness in this version. MCP comfy_* tools
(search templates, run_template, list_saved_workflows, …) fail against a
typical local install and burn the agent step budget. The live path is
Calliope's HTTP queue: list_workflows / run_workflow / enqueue_*.

To re-enable: add this module back to plugins.__init__._PLUGIN_MODULES and
call register() from harness.build_harness.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

from anyio import BrokenResourceError, ClosedResourceError

from calliope.agent.harness.registry import ToolContext, ToolDefinition, ToolRegistry
from calliope.config import settings

logger = logging.getLogger("calliope.harness.plugins.comfy_mcp")


async def _close_quietly(resource: Any) -> None:
    """Best-effort async-context teardown; never raises."""
    if resource is None:
        return
    try:
        await resource.__aexit__(None, None, None)
    except Exception:
        pass

# ── Tool sets for metadata ──────────────────────────────────────────────────

# Destructive tools: modify state, cancel jobs, stop/start ComfyUI, install stuff.
_DESTRUCTIVE_TOOL_NAMES = frozenset({
    "run_workflow",
    "generate_image",
    "partner_generate",
    "emit_partner_workflow",
    "run_template",
    "upload_file",
    "fetch_outputs",
    "launch_comfyui",
    "stop_comfyui",
    "restart_comfyui",
    "update_comfyui",
    "switch_comfyui_version",
    "install_node",
    "download_model",
    "save_workflow",
    "update_workflow",
    "share_workflow",
    "import_shared_workflow",
    "create_app",
    "auth_login",
    "free_memory",
})

# Tools we skip entirely (not useful in the harness context).
_SKIP_TOOLS = frozenset({
    "submit_feedback",
    "report_session_summary",
})

# Generation tools: actually run ComfyUI and produce images/videos. These are
# human-in-the-loop — the render-approval guard blocks them unless the user
# explicitly asked for generation. (File-transfer / workflow-writing tools that
# do not themselves render are deliberately left off this list.)
_GENERATION_TOOL_NAMES = frozenset({
    "run_workflow",
    "generate_image",
    "partner_generate",
    "run_template",
})


# ── Lazy MCP client singleton ───────────────────────────────────────────────


class ComfyMcpClient:
    """Async wrapper around a comfy-mcp stdio subprocess.

    Lazily spawns ``comfy-mcp`` on first use and auto-reconnects after the
    subprocess dies: a write-side pipe failure (request never reached the
    server) is retried once on a fresh connection; any other failure marks
    the session dead so the next call reconnects — retrying an app-level
    error is never done, since the tool may have already run server-side
    (double-executing a destructive tool like ``run_workflow``).
    All public methods are safe to call concurrently (guarded by a lock).
    """

    def __init__(self) -> None:
        self._session: Any = None
        self._ctx: Any = None
        self._lock = asyncio.Lock()
        self._tools_cache: list[dict[str, Any]] | None = None

    async def _connect(self) -> None:
        """Spawn comfy-mcp and initialise the MCP session (under lock)."""
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "comfy_mcp"],
            env={
                **os.environ,
                "COMFYUI_URL": settings.comfyui_base_url,
            },
        )
        logger.info("Spawning comfy-mcp subprocess …")
        ctx = stdio_client(params)
        read_stream, write_stream = await ctx.__aenter__()
        session = ClientSession(read_stream, write_stream)
        try:
            await session.__aenter__()
            await session.initialize()
        except Exception:
            # Session init failed — close what we opened so the subprocess
            # (task group, pipes) doesn't leak.
            await _close_quietly(session)
            await _close_quietly(ctx)
            raise
        self._ctx = ctx
        self._session = session
        logger.info("comfy-mcp session initialised")

    async def _ensure_connected(self) -> Any:
        """Return a live ``ClientSession``, reconnecting if needed."""
        if self._session is not None:
            return self._session
        async with self._lock:
            if self._session is not None:
                return self._session
            try:
                await self._connect()
            except Exception:
                logger.exception("Failed to connect to comfy-mcp")
                await self._teardown()
                raise
            return self._session

    async def _teardown(self) -> None:
        """Drop the current session/ctx (best-effort) so the next call
        reconnects with a fresh subprocess."""
        session, ctx = self._session, self._ctx
        self._session = None
        self._ctx = None
        self._tools_cache = None
        await _close_quietly(session)
        await _close_quietly(ctx)

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return the MCP tool list (cached after first successful fetch)."""
        if self._tools_cache is not None:
            return self._tools_cache
        try:
            session = await self._ensure_connected()
            result = await session.list_tools()
        except Exception:
            # A dead pipe here poisons nothing: force reconnect next call.
            await self._teardown()
            raise
        self._tools_cache = [
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": getattr(t, "inputSchema", getattr(t, "input_schema", {})),
            }
            for t in result.tools
        ]
        return self._tools_cache

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call an MCP tool by name.  Returns the parsed result."""
        retried = False
        while True:
            session = await self._ensure_connected()
            try:
                result = await session.call_tool(name, arguments)
            except (ClosedResourceError, BrokenResourceError) as exc:
                # Write-side failure: the request never reached the server,
                # so one reconnect + retry is safe.
                if retried:
                    raise
                retried = True
                logger.warning("comfy-mcp pipe died (%s); reconnecting", exc)
                await self._teardown()
                continue
            except Exception:
                # App-level or ambiguous failure: never retry inline (the
                # tool may have already executed), but mark the session
                # unhealthy so the next call starts fresh.
                await self._teardown()
                raise
            parts: list[Any] = []
            for block in result.content:
                if hasattr(block, "text"):
                    try:
                        parts.append(json.loads(block.text))
                    except (ValueError, TypeError):
                        parts.append(block.text)
                elif hasattr(block, "data"):
                    parts.append(
                        f"[{getattr(block, 'type', 'binary')} data, {len(block.data)} bytes]"
                    )
            if len(parts) == 1:
                return parts[0]
            return parts

    async def close(self) -> None:
        """Tear down the session and subprocess."""
        await self._teardown()


# Module-level singleton (lazy).
_client: ComfyMcpClient | None = None


def _get_client() -> ComfyMcpClient:
    global _client
    if _client is None:
        _client = ComfyMcpClient()
    return _client


# ── Executor factory ────────────────────────────────────────────────────────


def _make_executor(mcp_tool_name: str):
    """Return an async executor that bridges to the named MCP tool."""

    async def _executor(ctx: ToolContext, args: dict[str, Any]) -> Any:
        client = _get_client()
        try:
            return await client.call_tool(mcp_tool_name, args)
        except Exception as exc:
            logger.warning("comfy-mcp tool %s failed: %s", mcp_tool_name, exc)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    _executor.__name__ = f"comfy_mcp_{mcp_tool_name}"
    _executor.__qualname__ = _executor.__name__
    return _executor


# ── Prompt section ──────────────────────────────────────────────────────────


async def _comfy_mcp_prompt(ctx: ToolContext) -> str | None:
    return (
        "COMFY-MCP TOOLS (direct-to-ComfyUI):\n"
        "Tools prefixed with comfy_* bypass Calliope's job queue and talk to "
        "the local ComfyUI via the comfy-mcp server (comfy-cli under the hood). "
        "Use them for:\n"
        "- comfy_server_info: check ComfyUI health and hardware\n"
        "- comfy_search_nodes / comfy_get_node / comfy_search_models: explore installed nodes and models\n"
        "- comfy_validate_workflow: pre-flight a workflow before running it\n"
        "- comfy_launch_comfyui / comfy_stop_comfyui: manage the ComfyUI process\n"
        "- comfy_run_workflow / comfy_fetch_outputs: run a workflow directly and collect outputs\n"
        "- comfy_install_node / comfy_update_comfyui: manage ComfyUI installation\n"
        "Prefer the queue-based render tools (enqueue_asset_jobs, enqueue_video_jobs, "
        "wait_for_jobs) for normal project asset/video generation — they integrate "
        "with Calliope's project model and job tracking."
    )


# ── Plugin registration ────────────────────────────────────────────────────

# Known MCP tools with their harness metadata.
# (mcp_name, description, category, requires_project, destructive)
_KNOWN_TOOLS: list[tuple[str, str, str, bool, bool]] = [
    # Discovery / read-only
    ("server_info", "Check the local ComfyUI/comfy-cli environment. Call first.", "system", False, False),
    ("auth_status", "Check Comfy Cloud credential status for partner-API nodes.", "system", False, False),
    ("search_templates", "Search pre-built workflow templates by text, tag, or media type.", "discovery", False, False),
    ("get_template", "Fetch a template's full workflow JSON.", "discovery", False, False),
    ("get_template_schema", "See which of a template's parameters can be overridden at run time.", "discovery", False, False),
    ("search_nodes", "Search installed ComfyUI nodes by text, category, or I/O types.", "discovery", False, False),
    ("get_node", "Get the complete input spec for a specific node.", "discovery", False, False),
    ("list_nodes", "List all installed ComfyUI node classes.", "discovery", False, False),
    ("search_models", "List model files available on disk.", "discovery", False, False),
    ("cql", "Run a CQL graph query for structural questions about nodes.", "discovery", False, False),
    ("get_prompting_guide", "Get prompt style and recommended settings per model family.", "discovery", False, False),
    ("validate_workflow", "Pre-flight a workflow against the live ComfyUI before running.", "discovery", False, False),
    ("workflow_deps", "Check which models/custom nodes a workflow needs.", "discovery", False, False),
    ("system_stats", "Get ComfyUI system stats (GPU, VRAM, RAM).", "system", False, False),
    ("free_memory", "Free ComfyUI memory (unload models).", "system", False, True),
    ("get_logs", "Read ComfyUI's captured launch output.", "system", False, False),
    ("discover", "Learn comfy-cli's own tool surface.", "system", False, False),
    ("which", "Show which comfy-cli command a query matches.", "system", False, False),
    ("get_billing_status", "Check Comfy Cloud credit balance and subscription tier.", "system", False, False),
    # Jobs
    ("job", "Inspect/wait/watch/cancel a submitted job (action: status|wait|watch|cancel|queue).", "comfy-jobs", False, False),
    # Lifecycle (destructive)
    ("launch_comfyui", "Start the local ComfyUI server.", "comfy-lifecycle", False, True),
    ("stop_comfyui", "Stop the local ComfyUI server.", "comfy-lifecycle", False, True),
    ("restart_comfyui", "Restart the local ComfyUI server.", "comfy-lifecycle", False, True),
    # Generation (destructive)
    ("run_workflow", "Run a ComfyUI workflow JSON file directly.", "comfy-run", False, True),
    ("generate_image", "Generate an image using a pre-built template.", "comfy-run", False, True),
    ("partner_generate", "Generate with a partner-API model (Flux, Grok, etc.).", "comfy-run", False, True),
    ("emit_partner_workflow", "Write a runnable workflow for a partner model.", "comfy-run", False, True),
    ("run_template", "Run a pre-built template by name.", "comfy-run", False, True),
    ("upload_file", "Upload input files for workflows.", "comfy-run", False, True),
    ("fetch_outputs", "Copy a finished job's outputs to a directory.", "comfy-run", False, True),
    # Saved workflows
    ("list_saved_workflows", "Browse saved workflows from Comfy Cloud.", "discovery", False, False),
    ("get_saved_workflow", "Inspect a saved workflow's nodes and inputs.", "discovery", False, False),
    ("save_workflow", "Save a workflow to Comfy Cloud.", "comfy-workflows", False, True),
    ("update_workflow", "Update an existing saved workflow.", "comfy-workflows", False, True),
    ("share_workflow", "Publish a saved workflow and return a share URL.", "comfy-workflows", False, True),
    ("import_shared_workflow", "Import a workflow from a share URL.", "comfy-workflows", False, True),
    ("get_app_mode_url", "Get the stable link for a workflow as a runnable app.", "discovery", False, False),
    ("get_workflow_canvas_url", "Get a link to open a workflow on Comfy Cloud canvas.", "discovery", False, False),
    # Install / update (destructive)
    ("install_node", "Install a custom node pack.", "comfy-lifecycle", False, True),
    ("update_comfyui", "Update ComfyUI (core or all packs).", "comfy-lifecycle", False, True),
    ("switch_comfyui_version", "Switch ComfyUI version.", "comfy-lifecycle", False, True),
    ("download_model", "Download a model file.", "comfy-lifecycle", False, True),
    # Auth
    ("auth_login", "Sign in to Comfy Cloud via browser OAuth.", "system", False, True),
]


def register(registry: ToolRegistry, prompts: Any = None) -> None:
    """Register comfy-mcp bridge tools into the harness registry.

    Called at harness build time.  Tools are registered from a fixed known set
    — no live comfy-mcp connection is required at registration.  The actual
    MCP tool schemas are fetched lazily on first call.  If comfy-mcp is
    unreachable at call time, the executor returns an error dict.
    """
    for mcp_name, desc, category, req_proj, destructive in _KNOWN_TOOLS:
        if mcp_name in _SKIP_TOOLS:
            continue
        name = f"comfy_{mcp_name}"
        if name in registry.tools:
            continue  # already registered by another plugin (e.g. render.py)
        registry.register(
            ToolDefinition(
                name=name,
                description=desc,
                parameters={"type": "object", "properties": {}},
                executor=_make_executor(mcp_name),
                requires_project=req_proj,
                category=category,
                destructive=destructive,
                requires_approval=mcp_name in _GENERATION_TOOL_NAMES,
            )
        )

    # Register prompt section (order 50 — after workspace digest 30 and discipline 40).
    if prompts is not None:
        prompts.register("comfy_mcp", 50, _comfy_mcp_prompt)
