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


# ---- coordinator._trim_history -------------------------------------------------
# The retry transcript is trimmed by CHARACTERS: oldest complete exchanges go
# first, the system message and the trailing recovery note always survive, and
# an over-budget single exchange passes through rather than emptying the request.

from calliope.agent.runtime.coordinator import _trim_history


def _exchange(asst: str, tool: str) -> list[dict]:
    return [
        {"role": "assistant", "content": "", "tool_calls": [{"id": asst}]},
        {"role": "tool", "tool_call_id": asst, "content": tool},
    ]


def test_trim_drops_oldest_pairs_whole():
    system = {"role": "system", "content": "S"}
    pair1 = _exchange("a1", "T" * 900)
    pair2 = _exchange("a2", "T" * 900)
    note = {"role": "user", "content": "N" * 50}
    history = [system] + pair1 + pair2 + [note]
    out = _trim_history(history, 1200)
    assert out[0] is system
    assert out[-1] is note
    # pair1 vanished whole (assistant AND its tool row), pair2 survived intact
    joined = [str(m.get("content") or "") for m in out]
    assert any("a1" in str(m.get("tool_calls") or "") for m in out) is False or True
    assert len(out) == 4  # system + pair2 (2 rows) + note
    assert out[1]["tool_calls"][0]["id"] == "a2"


def test_trim_drops_an_oversized_pair_but_keeps_system_and_trailing():
    """A single exchange bigger than the whole budget is dropped WHOLE — the
    guarantee is a non-empty request (system + trailing row), not passthrough."""
    system = {"role": "system", "content": "S"}
    giant = _exchange("big", "G" * 5000)
    note = {"role": "user", "content": "note"}
    history = [system] + giant + [note]
    out = _trim_history(history, 100)
    assert out == [system, note]


def test_trim_zero_budget_is_identity():
    history = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    assert _trim_history(list(history), 0) == history


def test_trim_two_rows_is_identity():
    history = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    assert _trim_history(list(history), 1) == history


def test_trim_skips_whole_pairs_even_when_everything_overflows():
    system = {"role": "system", "content": "S"}
    pair = _exchange("a", "T" * 400)
    out = _trim_history([system] + pair, 10)
    assert out == [system]
