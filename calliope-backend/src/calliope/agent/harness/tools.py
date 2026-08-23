"""Compat shim over the plugin registry (deepseek-harness-style plugins).

The old module-level TOOLS dict + functions are preserved so existing imports
(`from calliope.agent.harness.tools import ToolContext, execute_tool,
openai_tools_payload, TOOLS`) keep working. New code should use
`calliope.agent.harness.get_registry()` / `get_prompts()` directly.
"""
from __future__ import annotations

from typing import Any

from calliope.agent.harness import build_harness
from calliope.agent.harness.registry import (  # re-export
    ToolContext,
    ToolDefinition,
    ToolRegistry,
)

_registry, _prompts = build_harness()

# Legacy shape: dict of name → ToolDefinition (duck-typed like the old Tool).
TOOLS: dict[str, ToolDefinition] = _registry.tools


def openai_tools_payload(ctx: ToolContext) -> list[dict[str, Any]]:
    """OpenAI tool-calling payload for the tools available in this context."""
    return _registry.openai_payload(ctx)


async def execute_tool(ctx: ToolContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run one tool by name through the guarded pipeline."""
    return await _registry.execute(ctx, name, args)


def build_system_prompt(ctx: ToolContext, workspace_digest: str = "") -> str:
    """Sync legacy builder: persona + mode sections only (no DB reads → safe
    to call sync). Async callers should use `get_prompts().assemble(ctx)`."""
    from calliope.agent.harness.prompts import _mode_text, _persona_text

    parts = [_persona_text(), _mode_text(ctx)]
    return "\n\n".join(p for p in parts if p)
