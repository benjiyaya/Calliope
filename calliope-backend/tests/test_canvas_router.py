"""Canvas v2.1 Phase 1 — CRUD router, get-or-create scoping, entity auto-seed."""
from __future__ import annotations

import calliope.config as config_module
from calliope.db import get_db


def _seed_story_entities(client, project_id: int) -> None:
    """Insert entity rows directly (no LLM) — deterministic test fixtures."""
    conn = get_db(config_module.settings.db_path)
    try:
        conn.execute(
            "INSERT INTO characters (project_id, name, appearance) VALUES (?, ?, ?)",
            (project_id, "Kira", "tall fighter"),
        )
        conn.execute(
            "INSERT INTO characters (project_id, name, appearance) VALUES (?, ?, ?)",
            (project_id, "Yun", "rival"),
        )
        conn.execute(
            "INSERT INTO locations (project_id, name, description) VALUES (?, ?, ?)",
            (project_id, "Rooftop", "night rooftop"),
        )
        conn.execute(
            "INSERT INTO items (project_id, name, description) VALUES (?, ?, ?)",
            (project_id, "Locket", "silver locket"),
        )
        conn.execute(
            "INSERT INTO scenes (project_id, order_index, heading, action) VALUES (?, ?, ?, ?)",
            (project_id, 1, "Round 1", "feint low, switch high"),
        )
        conn.commit()
    finally:
        conn.close()


def test_get_or_create_project_canvas(client):
    pid = client.post("/api/projects", json={"title": "Alpha", "idea": "x"}).json()["id"]
    r1 = client.post("/api/canvas", json={"project_id": pid, "title": "Alpha Bible"})
    assert r1.status_code == 200
    r2 = client.post("/api/canvas", json={"project_id": pid})
    assert r2.status_code == 200
    assert r1.json()["canvas"]["id"] == r2.json()["canvas"]["id"], "must be get-or-create"
    assert r2.json()["canvas"]["title"] == "Alpha Bible", "existing title preserved"


def test_sandbox_canvas_requires_session_and_scopes(client):
    sid = client.post("/api/agent/sessions", json={}).json()["id"]
    r = client.post("/api/canvas", json={"agent_session_id": sid})
    assert r.status_code == 200
    c = r.json()["canvas"]
    assert c["project_id"] is None
    assert c["agent_session_id"] == sid


def test_create_rejects_both_and_neither_scope(client):
    pid = client.post("/api/projects", json={"title": "Beta", "idea": "x"}).json()["id"]
    sid = client.post("/api/agent/sessions", json={}).json()["id"]
    assert client.post("/api/canvas", json={}).status_code == 422
    assert (
        client.post("/api/canvas", json={"project_id": pid, "agent_session_id": sid}).status_code
        == 422
    )


def test_linked_session_cannot_make_sandbox_canvas(client):
    pid = client.post("/api/projects", json={"title": "Linked", "idea": "x"}).json()["id"]
    sid = client.post("/api/agent/sessions", json={"project_id": pid}).json()["id"]
    r = client.post("/api/canvas", json={"agent_session_id": sid})
    assert r.status_code == 422


def test_auto_seed_and_position_persistence(client):
    pid = client.post("/api/projects", json={"title": "Gamma", "idea": "x"}).json()["id"]
    _seed_story_entities(client, pid)
    r = client.post("/api/canvas", json={"project_id": pid})
    assert r.status_code == 200, r.text
    graph = r.json()
    nodes = graph["nodes"]
    assert len(nodes) == 5, "2 chars + 1 loc + 1 item + 1 scene"
    kinds = {n["entity_type"] for n in nodes}
    assert kinds == {"character", "location", "item", "scene"}
    assert all(n["type"] == "entity" for n in nodes)

    seen: set[tuple[str, int]] = set()
    for n in nodes:
        key = (n["entity_type"], n["entity_id"])
        assert key not in seen, "duplicate entity node"
        seen.add(key)

    chars = [n for n in nodes if n["entity_type"] == "character"]
    locs = [n for n in nodes if n["entity_type"] == "location"]
    assert chars[0]["x"] < locs[0]["x"], "characters left of locations"

    node_id = nodes[0]["id"]
    r = client.patch(
        f"/api/canvas/{graph['canvas']['id']}/nodes/{node_id}", json={"x": 555.5, "y": 77}
    )
    assert r.status_code == 200
    assert abs(r.json()["x"] - 555.5) < 1e-9
    fetched = client.get(f"/api/canvas/{graph['canvas']['id']}").json()
    match = [n for n in fetched["nodes"] if n["id"] == node_id][0]
    assert match["x"] == 555.5


def test_reopen_seeds_only_new_entities(client):
    pid = client.post("/api/projects", json={"title": "Delta", "idea": "x"}).json()["id"]
    _seed_story_entities(client, pid)
    graph = client.post("/api/canvas", json={"project_id": pid}).json()
    canvas_id = graph["canvas"]["id"]
    before = {(n["entity_type"], n["entity_id"]) for n in graph["nodes"]}
    assert len(before) == 5

    conn = get_db(config_module.settings.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO characters (project_id, name, appearance) VALUES (?, ?, ?)",
            (pid, "Late Addition", "short"),
        )
        late_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    graph2 = client.post("/api/canvas", json={"project_id": pid}).json()
    assert graph2["canvas"]["id"] == canvas_id
    after = {(n["entity_type"], n["entity_id"]) for n in graph2["nodes"]}
    assert ("character", late_id) in after, "new entity must gain a node on reopen"
    assert before <= after
    assert len(after) == 6


def test_deleted_entity_node_not_reseeded(client):
    """Removing a node is a user choice — reopen must not resurrect it."""
    pid = client.post("/api/projects", json={"title": "Epsilon", "idea": "x"}).json()["id"]
    _seed_story_entities(client, pid)
    graph = client.post("/api/canvas", json={"project_id": pid}).json()
    canvas_id = graph["canvas"]["id"]
    victim = [n for n in graph["nodes"] if n["entity_type"] == "item"][0]
    assert client.delete(f"/api/canvas/{canvas_id}/nodes/{victim['id']}").status_code == 200

    graph2 = client.post("/api/canvas", json={"project_id": pid}).json()
    keys = {(n["entity_type"], n["entity_id"]) for n in graph2["nodes"]}
    assert ("item", victim["entity_id"]) not in keys


def test_viewport_and_title_patch(client):
    sid = client.post("/api/agent/sessions", json={}).json()["id"]
    cid = client.post("/api/canvas", json={"agent_session_id": sid}).json()["canvas"]["id"]
    r = client.patch(
        f"/api/canvas/{cid}",
        json={"title": "Scratch", "viewport_json": '{"x":12,"y":-4,"zoom":0.8}'},
    )
    assert r.status_code == 200
    assert r.json()["title"] == "Scratch"
    fetched = client.get(f"/api/canvas/{cid}").json()["canvas"]
    assert fetched["viewport_json"] == '{"x":12,"y":-4,"zoom":0.8}'


def test_viewport_must_be_json(client):
    sid = client.post("/api/agent/sessions", json={}).json()["id"]
    cid = client.post("/api/canvas", json={"agent_session_id": sid}).json()["canvas"]["id"]
    r = client.patch(f"/api/canvas/{cid}", json={"viewport_json": "not json{"})
    assert r.status_code == 422


def test_delete_canvas(client):
    sid = client.post("/api/agent/sessions", json={}).json()["id"]
    cid = client.post("/api/canvas", json={"agent_session_id": sid}).json()["canvas"]["id"]
    assert client.delete(f"/api/canvas/{cid}").status_code == 200
    assert client.get(f"/api/canvas/{cid}").status_code == 404


def test_canvas_updated_events_published(client, monkeypatch):
    """Graph mutations publish canvas.updated so open canvases refetch."""
    events: list[tuple[str, dict]] = []

    async def fake_publish(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    import calliope.events.bus as bus_mod

    monkeypatch.setattr(bus_mod.event_bus, "publish", fake_publish)

    pid = client.post("/api/projects", json={"title": "Ev", "idea": "x"}).json()["id"]
    r = client.post("/api/canvas", json={"project_id": pid})
    assert r.status_code == 200
    updates = [d for (t, d) in events if t == "canvas.updated"]
    assert updates and updates[-1]["canvas_id"] == r.json()["canvas"]["id"]
