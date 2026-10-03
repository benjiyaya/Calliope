"""History budget setting: API roundtrip + field validation (finding 7)."""
from __future__ import annotations


def test_history_budget_roundtrip(client, monkeypatch):
    """POST /api/settings persists agent_history_char_budget; GET reflects it."""
    r = client.post("/api/settings", json={"agent_history_char_budget": 120_000})
    assert r.status_code == 200
    assert r.json()["agent_history_char_budget"] == 120_000
    r2 = client.get("/api/settings")
    assert r2.status_code == 200
    assert r2.json()["agent_history_char_budget"] == 120_000


def test_history_budget_rejects_below_floor(client):
    """ge=0 — the floor used to be 10_000, but 0 now means 'derive it'."""
    r = client.post("/api/settings", json={"agent_history_char_budget": -1})
    assert r.status_code == 422


def test_history_budget_accepts_zero_as_auto(client):
    """0 is the auto mode, so it must round-trip rather than be rejected."""
    r = client.post("/api/settings", json={"agent_history_char_budget": 0})
    assert r.status_code == 200
    assert r.json()["agent_history_char_budget"] == 0


def test_history_budget_rejects_above_ceiling(client):
    r = client.post("/api/settings", json={"agent_history_char_budget": 3_000_000})
    assert r.status_code == 422
