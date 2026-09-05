"""Tests for run_workflow auto-attach (image → Project Assets) and the
cancel UI-state fix (runner publishes idle on cancel)."""
from __future__ import annotations

import asyncio

import pytest

from calliope.agent.harness.plugins.render import _resolve_attach_target
from calliope.agent.harness.registry import ToolContext


@pytest.fixture
def linked(client):
    """A session linked to a project with one character/location/item."""
    project = client.post(
        "/api/projects",
        json={"title": "Attach Test", "idea": "test", "genre": "drama", "tone": "dark"},
    ).json()
    char = client.post(f"/api/projects/{project['id']}/characters", json={"name": "Kira"}).json()
    loc = client.post(f"/api/projects/{project['id']}/locations", json={"name": "Dock"}).json()
    item = client.post(f"/api/projects/{project['id']}/items", json={"name": "Key"}).json()
    resp = client.post("/api/agent/sessions", json={})
    session = resp.json()
    client.patch(f"/api/agent/sessions/{session['id']}", json={"project_id": project["id"]})
    return {
        "project": project,
        "character": char,
        "location": loc,
        "item": item,
        "session": session,
        "ctx": ToolContext(session_id=session["id"], project_id=project["id"]),
    }


def test_attach_by_character_id(linked):
    payload, note = _resolve_attach_target(
        linked["ctx"], {"character_id": linked["character"]["id"]}
    )
    assert payload == {
        "character_id": linked["character"]["id"],
        "asset_target": "sheet",
    }
    assert "Kira" in note


def test_attach_by_location_id(linked):
    payload, note = _resolve_attach_target(
        linked["ctx"], {"location_id": linked["location"]["id"]}
    )
    assert payload == {"location_id": linked["location"]["id"]}
    assert "Dock" in note


def test_attach_by_item_id(linked):
    payload, note = _resolve_attach_target(linked["ctx"], {"item_id": linked["item"]["id"]})
    assert payload == {"item_id": linked["item"]["id"]}


def test_attach_by_name(linked):
    payload, note = _resolve_attach_target(
        linked["ctx"], {"attach": {"target": "character_sheet", "name": "kira"}}
    )
    assert payload == {
        "character_id": linked["character"]["id"],
        "asset_target": "sheet",
    }


def test_attach_wrong_project_refuses(linked, client):
    other = client.post("/api/projects", json={"title": "Other"}).json()
    ctx = ToolContext(session_id=linked["session"]["id"], project_id=other["id"])
    payload, note = _resolve_attach_target(ctx, {"character_id": linked["character"]["id"]})
    assert payload == {}
    assert "NOT be auto-filed" in note


def test_no_attach_args_is_noop(linked):
    payload, note = _resolve_attach_target(linked["ctx"], {})
    assert payload == {}
    assert note is None


def test_cancel_publishes_idle(client):
    """After cancel, the session must be idle + running=False without a page
    refresh (the SSE event the UI relies on is published by the endpoint)."""
    resp = client.post("/api/agent/sessions", json={})
    session = resp.json()
    # No run is active: cancel returns False but must not corrupt state.
    resp = client.post(f"/api/agent/sessions/{session['id']}/cancel")
    assert resp.status_code == 200
    detail = client.get(f"/api/agent/sessions/{session['id']}").json()
    assert detail["running"] is False
    assert detail["status"] == "idle"
