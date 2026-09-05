"""Tests for the memory plugin: tools, scoping, prompt-section recall."""
from __future__ import annotations

import asyncio

import pytest

from calliope.agent.harness import log as session_log
from calliope.agent.harness.plugins.memory import (
    CONTENT_CAP,
    PROMPT_ITEM_CAP,
    memory_prompt_text,
)
from calliope.agent.harness.registry import ToolContext
from calliope.agent.harness.tools import execute_tool


@pytest.fixture
def session(client):
    resp = client.post("/api/agent/sessions", json={})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_save_list_forget_roundtrip(session):
    ctx = ToolContext(session_id=session["id"], project_id=None)
    saved = _run(execute_tool(ctx, "save_memory", {"content": "User prefers terse scene descriptions", "scope": "global"}))
    assert saved["ok"] is True
    listed = _run(execute_tool(ctx, "list_memories", {}))
    assert listed["count"] == 1
    assert listed["memories"][0]["content"] == "User prefers terse scene descriptions"
    forgotten = _run(execute_tool(ctx, "forget_memory", {"memory_id": saved["memory_id"]}))
    assert forgotten["ok"] is True
    listed_after = _run(execute_tool(ctx, "list_memories", {}))
    assert listed_after["count"] == 0


def test_save_memory_events_appended(session):
    ctx = ToolContext(session_id=session["id"], project_id=None)
    saved = _run(execute_tool(ctx, "save_memory", {"content": "Answers in English", "scope": "global"}))
    _run(execute_tool(ctx, "forget_memory", {"memory_id": saved["memory_id"]}))
    types = [e.type for e in session_log.read_events(session["id"])]
    assert session_log.MEMORY_SAVED in types
    assert session_log.MEMORY_FORGOTTEN in types


def test_content_cap_enforced(session):
    ctx = ToolContext(session_id=session["id"], project_id=None)
    result = _run(execute_tool(ctx, "save_memory", {"content": "x" * (CONTENT_CAP + 1), "scope": "global"}))
    assert result["ok"] is False


def test_upsert_deduplicates(session):
    ctx = ToolContext(session_id=session["id"], project_id=None)
    first = _run(execute_tool(ctx, "save_memory", {"content": "Always H3 profile", "scope": "global"}))
    second = _run(execute_tool(ctx, "save_memory", {"content": "Always H3 profile", "scope": "global"}))
    assert second["deduplicated"] is True
    assert second["memory_id"] == first["memory_id"]
    listed = _run(execute_tool(ctx, "list_memories", {}))
    assert listed["count"] == 1


def test_project_scope_hidden_in_sandbox(client):
    # project-linked session saves a project memory
    resp = client.post("/api/agent/sessions", json={})
    s1 = resp.json()
    project = client.post("/api/projects", json={"title": "P1"}).json()
    client.patch(
        f"/api/agent/sessions/{s1['id']}", json={"project_id": project["id"]}
    ) if False else None
    ctx_linked = ToolContext(session_id=s1["id"], project_id=project["id"])
    _run(execute_tool(ctx_linked, "save_memory", {"content": "Antagonist never in daylight", "scope": "project"}))

    # a different session (sandbox) sees nothing project-scoped, only global
    resp2 = client.post("/api/agent/sessions", json={})
    s2 = resp2.json()
    ctx_sandbox = ToolContext(session_id=s2["id"], project_id=None)
    _run(execute_tool(ctx_sandbox, "save_memory", {"content": "Global preference", "scope": "global"}))
    listed = _run(execute_tool(ctx_sandbox, "list_memories", {}))
    contents = [m["content"] for m in listed["memories"]]
    assert "Global preference" in contents
    assert "Antagonist never in daylight" not in contents


def test_prompt_section_renders_and_bumps_usage(session):
    ctx = ToolContext(session_id=session["id"], project_id=None)
    for i in range(3):
        _run(execute_tool(ctx, "save_memory", {"content": f"Preference number {i}", "scope": "global"}))
    text = memory_prompt_text(ctx)
    assert text is not None
    assert text.startswith("## Memory")
    for i in range(3):
        assert f"Preference number {i}" in text
    # rendering counts as usage
    from calliope.agent.harness.registry import _db
    from calliope.config import settings

    conn = _db()
    try:
        rows = conn.execute("SELECT use_count FROM agent_memory").fetchall()
        assert all(r["use_count"] == 1 for r in rows)
    finally:
        conn.close()


def test_prompt_section_none_when_empty(session):
    ctx = ToolContext(session_id=session["id"], project_id=None)
    assert memory_prompt_text(ctx) is None


def test_prompt_section_respects_item_cap(session):
    ctx = ToolContext(session_id=session["id"], project_id=None)
    for i in range(PROMPT_ITEM_CAP + 6):
        _run(execute_tool(ctx, "save_memory", {"content": f"Memory item {i:03d}", "scope": "global"}))
    text = memory_prompt_text(ctx)
    assert text.count("\n- ") <= PROMPT_ITEM_CAP


def test_settings_memory_api(client):
    """The Settings → Agent memory management endpoints: list across scopes,
    add (source='user'), delete."""
    project = client.post("/api/projects", json={"title": "Memory Film"}).json()
    pid = project["id"]

    added = client.post(
        "/api/agent/memories",
        json={"content": "User prefers 16:9 aspect", "scope": "global", "kind": "preference"},
    )
    assert added.status_code == 200, added.text
    gid = added.json()["id"]

    added_p = client.post(
        "/api/agent/memories",
        json={"content": "Antagonist never shown in daylight", "scope": "project", "project_id": pid},
    )
    assert added_p.status_code == 200, added_p.text

    listing = client.get("/api/agent/memories")
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 2
    g = next(r for r in rows if r["id"] == gid)
    assert g["source"] == "user"
    assert g["scope"] == "global"
    p = next(r for r in rows if r["scope"] == "project")
    assert p["project_title"] == "Memory Film"

    # validation
    bad = client.post("/api/agent/memories", json={"content": "  ", "scope": "global"})
    assert bad.status_code == 422
    bad_scope = client.post(
        "/api/agent/memories", json={"content": "x", "scope": "galaxy"}
    )
    assert bad_scope.status_code == 422

    gone = client.delete(f"/api/agent/memories/{gid}")
    assert gone.status_code == 200
    assert client.delete(f"/api/agent/memories/{gid}").status_code == 404
    assert len(client.get("/api/agent/memories").json()) == 1
