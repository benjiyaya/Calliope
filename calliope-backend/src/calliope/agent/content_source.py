"""Who writes generated content for a tool call: Calliope's LLM or the MCP client.

Settings → Agent → "MCP content source". For calls that arrive over MCP
(ToolContext origin "mcp", see calliope.mcp_server) the default is the MCP
client: story, script, shots, continuity plan and H3 prompts are supplied by
the client (e.g. Claude Code) and Calliope's own LLM is never called. The
generation tools then work in two steps — without ``content`` they return a
brief (the exact instructions and context Calliope's LLM would have received),
with ``content`` they validate and save it through the same code paths.
In-app calls (chat agent, UI buttons) always use Calliope's LLM.
"""
from __future__ import annotations

from typing import Any

from calliope.config import settings

CLIENT = "client"
CALLIOPE = "calliope"


def client_supplies_content(ctx: Any) -> bool:
    """True when this tool call must not use Calliope's LLM."""
    return getattr(ctx, "origin", None) == "mcp" and settings.mcp_content_source != CALLIOPE


def brief_result(tool: str, brief: dict[str, Any], *, content_hint: str) -> dict[str, Any]:
    """Uniform reply for step 1 of a client-content tool."""
    return {
        "ok": True,
        "needs_content": True,
        "brief": brief,
        "next": f"Write the content, then call {tool} again with {content_hint}.",
    }
