"""Contract vocabulary export: single registry of names shared across layers.

Calliope's backend and frontend communicate through stringly-typed contracts
(agent tool names, harness event types, SSE bus events, ComfyUI roles,
prompt profiles). Nothing type-checks across that boundary, so a rename ships
silently until a user clicks. This script emits the positive allowlist —
`contracts.json` — consumed by tests/test_contract_vocabulary.py, which fails
when the web bundle references a name that no longer exists.

The agent engine is runtime/ (schedule → coordinator → subagent per grant) +
capabilities/ (catalog.py owns the GRANTS dict — the tool surface). The
exporter PARSES sources, never imports, so it runs without the venv.

Regenerate after ANY change to: roles.py alias tables, log.py / runner.py /
routers/agent.py event types, catalog.py GRANTS or shot_ops._SHOT_NAMES,
event_bus.publish("…"), prompt profiles:

    python scripts/export_contracts.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "calliope"
OUT = ROOT / "contracts.json"

# ── roles (from comfyui/roles.py — parsed, not imported, so this script
#    never needs the venv) ────────────────────────────────────────────────
_ROLE_BLOCK = re.compile(
    r"(INPUT|OUTPUT)_ROLE_ALIASES[^=]*=\s*\{(.*?)\n\}", re.DOTALL
)


def _extract_roles() -> dict[str, list[str]]:
    text = (SRC / "comfyui" / "roles.py").read_text(encoding="utf-8")
    roles: dict[str, list[str]] = {"input": [], "output": []}
    for kind, block in _ROLE_BLOCK.findall(text):
        key = kind.lower()
        for canon, aliases in re.findall(r'"([a-z0-9_-]+)"\s*:\s*frozenset\(\{(.*?)\}\)', block, re.DOTALL):
            names = [canon] + re.findall(r'"([a-z0-9_-]+)"', aliases)
            roles[key].extend(sorted(set(names)))
    return roles


def _extract_event_types() -> list[str]:
    """Agent event vocabulary: constants in harness/log.py plus every
    append_event("…") literal and slash-typed string across the agent package
    (runner computes some types, e.g. task/start vs task/end)."""
    events: set[str] = set()
    log_text = (SRC / "agent" / "harness" / "log.py").read_text(encoding="utf-8")
    events.update(re.findall(r'^[A-Z_]+ = "([a-z/]+)"', log_text, re.MULTILINE))
    literal = re.compile(r'append_event\(\s*[^,]+,\s*\n?\s*"([a-z/]+)"')
    typed = re.compile(
        r'["\']((?:user|assistant|tool|steering|plan|task|turn|step|question|memory)/[a-z_]+)["\']'
    )
    for py in (SRC / "agent").rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        events.update(literal.findall(text))
        events.update(typed.findall(text))
    return sorted(events)


_GRANT_NAMES = {"story", "script", "assets", "render", "canvas", "shot"}


def _extract_tool_names() -> list[str]:
    """Every name in catalog.GRANTS (+ shot_ops._SHOT_NAMES referenced from it).

    Parsed from source so the script runs without the venv: the GRANTS dict
    literal, the _MEMORY/_SYSTEM unions, and the shot-name tuple.
    """
    catalog = (SRC / "agent" / "capabilities" / "catalog.py").read_text(encoding="utf-8")
    match = re.search(
        r"GRANTS: dict\[str, frozenset\[str\]\] = \{(.*?)\n_PROMPTS", catalog, re.DOTALL
    )
    if match is None:
        raise SystemExit("catalog.py no longer matches the GRANTS literal layout")
    names: set[str] = set(re.findall(r'"([a-z_]+)"', match.group(1)))
    names -= _GRANT_NAMES
    shot_ops = (SRC / "agent" / "capabilities" / "shot_ops.py").read_text(encoding="utf-8")
    shot_match = re.search(r"_SHOT_NAMES = \((.*?)\)", shot_ops, re.DOTALL)
    if shot_match is not None:
        names.update(re.findall(r'"([a-z_]+)"', shot_match.group(1)))
    return sorted(names)


def _extract_sse_events() -> list[str]:
    """Every literal published on the SSE bus, across the whole backend."""
    events: set[str] = set()
    pattern = re.compile(r'(?:event_bus\.publish|publish_user_event)\(\s*\n?\s*"([a-z_.]+)"')
    for py in SRC.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        events.update(pattern.findall(text))
    return sorted(events)


def _extract_prompt_profiles() -> list[str]:
    text = (SRC / "comfyui" / "profiles.py").read_text(encoding="utf-8") if (SRC / "comfyui" / "profiles.py").exists() else ""
    if not text:
        # profiles may live elsewhere; fall back to the canonical two from AGENTS.md
        return ["prose", "minimax_h3_ref"]
    return sorted(set(re.findall(r'"(prose|minimax_h3_ref)"', text)) or {"prose", "minimax_h3_ref"})


def build_registry() -> dict[str, list[str]]:
    return {
        "input_roles": _extract_roles()["input"],
        "output_roles": _extract_roles()["output"],
        "agent_event_types": _extract_event_types(),
        "agent_tool_names": _extract_tool_names(),
        "sse_events": _extract_sse_events(),
        "prompt_profiles": _extract_prompt_profiles(),
    }


def main() -> int:
    registry = build_registry()
    OUT.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
    total = sum(len(v) for v in registry.values())
    print(f"wrote {OUT.relative_to(ROOT)} ({total} names)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
