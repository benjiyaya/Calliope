"""Harness plugins: each module contributes tools / prompt sections / hooks.

Load order matters only for prompt-section registration ties; tool names are
unique across plugins (enforced by the registry).
"""
from calliope.agent.harness import plugins  # noqa: F401

# comfy_mcp is shipped but not loaded: MCP comfy_* tools fail in this
# environment and the model wastes the step budget on them. Re-add the
# module here when the comfy-mcp server is a supported path again.
_PLUGIN_MODULES = (
    "calliope.agent.harness.plugins.workspace",
    "calliope.agent.harness.plugins.story",
    "calliope.agent.harness.plugins.script",
    "calliope.agent.harness.plugins.render",
    "calliope.agent.harness.plugins.canvas",
    "calliope.agent.harness.plugins.interaction",
    "calliope.agent.harness.plugins.memory",
    "calliope.agent.harness.plugins.skills",
)


def load_all() -> None:
    import importlib

    for mod_name in _PLUGIN_MODULES:
        importlib.import_module(mod_name)
