"""E2E tests for agent session linking: event payload enrichment, link
picker endpoint, and unlink round-trip."""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def captured_events(monkeypatch):
    events: list[tuple[str, dict]] = []

    async def fake_publish(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    import calliope.events.bus as bus_mod

    monkeypatch.setattr(bus_mod.event_bus, "publish", fake_publish)
    return events


def _make_project(client, title: str) -> int:
    r = client.post("/api/projects", json={"title": title, "idea": f"{title} idea"})
    assert r.status_code == 200
    return r.json()["id"]


def test_linkable_projects_endpoint(client):
    pid = _make_project(client, "Alpha")
    _make_project(client, "Beta")
    r = client.get("/api/agent/projects")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    assert all(set(p) == {"id", "title", "status"} for p in data)
    assert any(p["id"] == pid and p["title"] == "Alpha" for p in data)


def test_session_updated_events_carry_project(client, captured_events):
    """Every agent.session.updated event must carry the enriched session with
    `project` info — the UI header depends on it to flip sandbox→linked."""
    session_events = lambda: [d for (t, d) in captured_events if t == "agent.session.updated"]

    # Create session (blind)
    r = client.post("/api/agent/sessions", json={})
    sid = r.json()["id"]
    create_events = session_events()
    assert create_events, "create_session must publish agent.session.updated"
    s = create_events[-1]["session"]
    assert s["id"] == sid
    assert s["project_id"] is None
    assert s["project"] is None
    assert s["running"] is False

    # Link via PATCH
    pid = _make_project(client, "Linked One")
    r = client.patch(f"/api/agent/sessions/{sid}", json={"project_id": pid})
    assert r.status_code == 200
    assert r.json()["project"]["title"] == "Linked One"
    patch_events = session_events()
    assert patch_events, "patch_session must publish agent.session.updated"
    s = patch_events[-1]["session"]
    assert s["project_id"] == pid
    assert s["project"] == {"id": pid, "title": "Linked One", "status": "draft"}

    # Unlink → project null again
    r = client.patch(f"/api/agent/sessions/{sid}", json={"unlink": True})
    assert r.status_code == 200
    s = session_events()[-1]["session"]
    assert s["project_id"] is None
    assert s["project"] is None


def test_create_project_tool_event_enriched(client, captured_events):
    """The harness's create_project/link_project auto-link path must also
    publish enriched sessions (this was the live-header bug)."""
    from calliope.agent.harness.tools import ToolContext, execute_tool

    r = client.post("/api/agent/sessions", json={})
    sid = r.json()["id"]

    ctx = ToolContext(session_id=sid, project_id=None)
    result = asyncio.run(execute_tool(ctx, "create_project", {"title": "Auto", "idea": "x"}))
    assert result["ok"] is True
    pid = result["project"]["id"]

    session_events = [d for (t, d) in captured_events if t == "agent.session.updated"]
    assert session_events, "auto-link must publish agent.session.updated"
    s = session_events[-1]["session"]
    assert s["id"] == sid
    assert s["project_id"] == pid
    assert s["project"] == {"id": pid, "title": "Auto", "status": "draft"}
