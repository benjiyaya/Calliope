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


def test_canvas_title_derived_when_omitted(client):
    """No title given: project canvases take the project's, sandbox canvases
    the session's (trimmed to 60 chars)."""
    long = "S" * 200  # project titles cap at 200 — still beyond the 60-char trim
    pid = client.post("/api/projects", json={"title": long, "idea": "x"}).json()["id"]
    g1 = client.post("/api/canvas", json={"project_id": pid}).json()["canvas"]
    assert g1["title"] == long[:60], "project title trimmed to 60 chars"

    sid = client.post("/api/agent/sessions", json={"title": "Neon Chase chat"}).json()["id"]
    g2 = client.post("/api/canvas", json={"agent_session_id": sid}).json()["canvas"]
    assert g2["title"] == "Neon Chase chat", "sandbox canvas named after the session"

    # Explicit title still wins.
    pid2 = client.post("/api/projects", json={"title": "Named", "idea": "x"}).json()["id"]
    g3 = client.post(
        "/api/canvas", json={"project_id": pid2, "title": "Custom"}
    ).json()["canvas"]
    assert g3["title"] == "Custom"


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


# ---- simplified preview-board layout + artifact tool ------------------------


def test_seed_layout_groups_in_gallery_columns(client):
    """Preview-board columns: chars | locs | items | artifacts | scenes."""
    pid = client.post("/api/projects", json={"title": "Layout", "idea": "x"}).json()["id"]
    _seed_story_entities(client, pid)
    graph = client.post("/api/canvas", json={"project_id": pid}).json()
    nodes = graph["nodes"]
    chars = [n for n in nodes if n["entity_type"] == "character"]
    locs = [n for n in nodes if n["entity_type"] == "location"]
    items = [n for n in nodes if n["entity_type"] == "item"]
    scenes = [n for n in nodes if n["entity_type"] == "scene"]
    assert all(n["x"] < m["x"] for n in chars for m in locs)
    assert all(n["x"] < m["x"] for n in locs for m in items)
    assert all(n["x"] < m["x"] for n in items for m in scenes)
    # artifact column sits between items and scenes
    artifact_x = 1000
    assert all(n["x"] < artifact_x < m["x"] for n in items for m in scenes)


def test_artifact_slot_fills_grid(client):
    from calliope.routers.canvas import (
        ARTIFACT_COL_WIDTH,
        ARTIFACT_GRID_X,
        next_artifact_slot,
    )

    pid = client.post("/api/projects", json={"title": "Stack", "idea": "x"}).json()["id"]
    cid = client.post("/api/canvas", json={"project_id": pid}).json()["canvas"]["id"]

    conn = get_db(config_module.settings.db_path)
    try:
        x, y = next_artifact_slot(conn, cid)
        assert x == ARTIFACT_GRID_X
        assert y == 80
        conn.execute(
            """
            INSERT INTO canvas_node
                (canvas_id, type, title, x, y, artifact_path, status, created_at, updated_at)
            VALUES (?, 'image', 'first', ?, ?, 'p.png', 'done', '2026-01-01', '2026-01-01')
            """,
            (cid, ARTIFACT_GRID_X, 80),
        )
        x2, y2 = next_artifact_slot(conn, cid)
    finally:
        conn.close()
    # Grid is row-major: the second card sits to the RIGHT in the same row
    assert x2 == ARTIFACT_GRID_X + ARTIFACT_COL_WIDTH
    assert y2 == 80


def test_create_node_auto_layout_when_xy_omitted(client):
    """POST nodes without x/y: backend resolves the artifact gallery slot."""
    pid = client.post("/api/projects", json={"title": "AutoXY", "idea": "x"}).json()["id"]
    cid = client.post("/api/canvas", json={"project_id": pid}).json()["canvas"]["id"]

    first = client.post(
        f"/api/canvas/{cid}/nodes",
        json={"type": "image", "title": "a", "artifact_path": "a.png"},
    ).json()
    second = client.post(
        f"/api/canvas/{cid}/nodes",
        json={"type": "video", "title": "b", "artifact_path": "b.mp4"},
    ).json()
    from calliope.routers.canvas import ARTIFACT_COL_WIDTH, ARTIFACT_GRID_X

    assert first["x"] == ARTIFACT_GRID_X
    # Row-major grid: the second card sits to the right in the same row
    assert second["x"] == first["x"] + ARTIFACT_COL_WIDTH
    assert second["y"] == first["y"]


def test_post_artifact_to_canvas_from_job(client, monkeypatch):
    """post_artifact_to_canvas resolves a finished job's output into a card."""
    import asyncio

    from calliope.agent.harness.plugins.canvas import t_post_artifact
    from calliope.agent.harness.registry import ToolContext

    pid = client.post("/api/projects", json={"title": "Post", "idea": "x"}).json()["id"]
    sid = client.post("/api/agent/sessions", json={"project_id": pid}).json()["id"]
    cid = client.post("/api/canvas", json={"project_id": pid}).json()["canvas"]["id"]

    conn = get_db(config_module.settings.db_path)
    try:
        conn.execute(
            """
            INSERT INTO jobs (id, project_id, kind, status, output_paths_json)
            VALUES (9001, ?, 'image', 'done', ?)
            """,
            (pid, '["C:/assets/out_0001_.png"]'),
        )
        conn.commit()
    finally:
        conn.close()

    async def fake_publish(event_type: str, data: dict) -> None:
        pass

    import calliope.events.bus as bus_mod

    monkeypatch.setattr(bus_mod.event_bus, "publish", fake_publish)

    ctx = ToolContext(session_id=sid, project_id=pid)
    result = asyncio.run(t_post_artifact(ctx, {"job_id": 9001}))
    assert result["ok"] is True, result
    assert result["type"] == "image"
    assert result["artifact_path"] == "C:/assets/out_0001_.png"

    graph = client.get(f"/api/canvas/{cid}").json()
    cards = [n for n in graph["nodes"] if n["type"] == "image"]
    assert len(cards) == 1
    assert cards[0]["job_id"] == 9001
    assert cards[0]["artifact_path"] == "C:/assets/out_0001_.png"
    from calliope.routers.canvas import ARTIFACT_GRID_X

    assert cards[0]["x"] == ARTIFACT_GRID_X, "card lands in the artifact grid"


def test_post_artifact_video_kind_from_asset_path(client, monkeypatch):
    """asset_path extension decides image vs video; no disk check needed."""
    import asyncio

    from calliope.agent.harness.plugins.canvas import t_post_artifact
    from calliope.agent.harness.registry import ToolContext

    pid = client.post("/api/projects", json={"title": "PostV", "idea": "x"}).json()["id"]
    sid = client.post("/api/agent/sessions", json={"project_id": pid}).json()["id"]
    cid = client.post("/api/canvas", json={"project_id": pid}).json()["canvas"]["id"]

    async def fake_publish(event_type: str, data: dict) -> None:
        pass

    import calliope.events.bus as bus_mod

    monkeypatch.setattr(bus_mod.event_bus, "publish", fake_publish)

    ctx = ToolContext(session_id=sid, project_id=pid)
    result = asyncio.run(
        t_post_artifact(ctx, {"asset_path": "C:/clips/scene_3.mp4", "title": "Scene 3"})
    )
    assert result["ok"] is True, result
    assert result["type"] == "video"

    graph = client.get(f"/api/canvas/{cid}").json()
    cards = [n for n in graph["nodes"] if n["type"] == "video"]
    assert len(cards) == 1
    assert cards[0]["title"] == "Scene 3"


def test_post_artifact_requires_job_or_asset(client):
    import asyncio

    from calliope.agent.harness.plugins.canvas import t_post_artifact
    from calliope.agent.harness.registry import ToolContext

    pid = client.post("/api/projects", json={"title": "PostN", "idea": "x"}).json()["id"]
    sid = client.post("/api/agent/sessions", json={"project_id": pid}).json()["id"]
    # Session needs a canvas for the scope check to pass before arg validation.
    client.post("/api/canvas", json={"project_id": pid})

    ctx = ToolContext(session_id=sid, project_id=pid)
    result = asyncio.run(t_post_artifact(ctx, {}))
    assert result["ok"] is False
    assert "job_id" in result["error"]
