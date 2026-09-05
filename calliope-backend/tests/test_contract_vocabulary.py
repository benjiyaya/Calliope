"""Contract vocabulary gate.

The backend and the web frontend share stringly-typed contracts — SSE event
names, agent tool names, ComfyUI roles, guard codes. Nothing type-checks
across that boundary, so a backend rename used to ship silently until a UI
action 404'd or an agent hint pointed at a tool that no longer exists.

This gate is a POSITIVE allowlist (mirrors comfyui-mcp-panel's
check-tool-vocabulary): calliope-web literals that look like contract members
must appear in contracts.json (regenerate with scripts/export_contracts.py).
An unknown name fails with file:line — at test time, not at user time.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
WEB = BACKEND.parent / "calliope-web" / "src"
CONTRACTS = json.loads((BACKEND / "contracts.json").read_text(encoding="utf-8"))

# SSE event names quoted in the web bundle, e.g. onEvent("job.completed", …)
_SSE_RE = re.compile(r'["\']((?:agent|job|canvas|asset|story|queue|session)\.[a-z_.]+)["\']')
# Guard codes consumed in web code or asserted in harness strings
_GUARD_RE = re.compile(r'["\'](guard_[a-z_]+)["\']')
# Role-chip literals: "Input:prompt" style hints in web canvas code
_ROLE_RE = re.compile(r'["\'](?:Input|Output):([a-z0-9_-]+)["\']')

# Documented contract names that legitimately appear in prose but must not
# silently rot: checked for presence in contracts.json only (documented = real).
KNOWN_TOOL_HINTS_RE = re.compile(r"\b(?:ask_user|save_memory|list_memories|forget_memory|list_skills|read_skill)\b")


def _web_sources() -> list[Path]:
    if not WEB.is_dir():
        return []
    return [p for p in WEB.rglob("*") if p.suffix in (".ts", ".svelte", ".js") and p.is_file()]


def test_web_references_only_known_sse_events():
    unknown: list[str] = []
    for path in _web_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for name in _SSE_RE.findall(text):
            if name not in CONTRACTS["sse_events"]:
                unknown.append(f"{path.relative_to(WEB)}: {name}")
    assert not unknown, "SSE events referenced in calliope-web but not published by the backend:\n" + "\n".join(unknown)


def test_web_references_only_known_guard_codes():
    unknown: list[str] = []
    for path in _web_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for name in _GUARD_RE.findall(text):
            if name not in CONTRACTS["guard_codes"]:
                unknown.append(f"{path.relative_to(WEB)}: {name}")
    assert not unknown, "guard codes referenced in calliope-web but not defined by the harness:\n" + "\n".join(unknown)


def test_web_role_chips_use_canonical_or_alias_roles():
    unknown: list[str] = []
    allowed = set(CONTRACTS["input_roles"]) | set(CONTRACTS["output_roles"])
    for path in _web_sources():
        text = path.read_text(encoding="utf-8", errors="replace")
        for role in _ROLE_RE.findall(text):
            if role not in allowed:
                unknown.append(f"{path.relative_to(WEB)}: {role}")
    assert not unknown, "role tags in calliope-web that roles.py does not define:\n" + "\n".join(unknown)


def test_registry_covers_core_contract_families():
    """The allowlist itself must stay whole — an empty/gutted registry makes
    every other check here vacuous."""
    assert len(CONTRACTS["sse_events"]) >= 10
    assert len(CONTRACTS["agent_tool_names"]) >= 20
    assert len(CONTRACTS["agent_event_types"]) >= 10
    assert "guard_render_approval" in CONTRACTS["guard_codes"]
    assert "guard_destructive_replace" in CONTRACTS["guard_codes"]
    for tool in ("ask_user", "save_memory", "list_skills", "read_skill", "run_workflow"):
        assert tool in CONTRACTS["agent_tool_names"], f"export_contracts.py lost tool {tool}"
    for event in ("question/asked", "question/answered", "memory/saved", "memory/forgotten"):
        assert event in CONTRACTS["agent_event_types"], f"export_contracts.py lost event {event}"


def test_contracts_file_matches_source():
    """Fail when source changed but contracts.json was not regenerated.

    Rebuilds the registry in-process and compares; the fix is always
    `python scripts/export_contracts.py`.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "export_contracts", BACKEND / "scripts" / "export_contracts.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    fresh = mod.build_registry()
    for key, values in fresh.items():
        assert set(CONTRACTS[key]) == set(values), (
            f"contracts.json[{key}] is stale — run: python scripts/export_contracts.py"
        )
