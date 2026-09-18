"""Canvas node deletion — real delete with file cleanup rules.

Sandbox artifact nodes (uploads/ + scratch outputs) hard-delete their file;
project-owned or still-referenced files survive the node delete.
"""
from __future__ import annotations

from pathlib import Path

import calliope.config as config_module
from calliope.db import get_db


def _seed_story_entities(project_id: int) -> None:
    conn = get_db(config_module.settings.db_path)
    try:
        conn.execute(
            "INSERT INTO characters (project_id, name, appearance) VALUES (?, ?, ?)",
            (project_id, "Kira", "tall fighter"),
        )
        conn.execute(
            "INSERT INTO scenes (project_id, order_index, heading, action) VALUES (?, ?, ?, ?)",
            (project_id, 1, "Round 1", "feint low, switch high"),
        )
        conn.commit()
    finally:
        conn.close()


def _ensure_sandbox_canvas(client) -> dict:
    sid = client.post("/api/agent/sessions", json={}).json()["id"]
    return client.post("/api/canvas", json={"agent_session_id": sid}).json()


def _create_artifact_node(canvas_id: int, path: str, *, job_id: int | None = None) -> int:
    conn = get_db(config_module.settings.db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO canvas_node (canvas_id, type, title, x, y, artifact_path, job_id, status)
            VALUES (?, 'image', 'Artifact', 0, 0, ?, ?, 'done')
            """,
            (canvas_id, path, job_id),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _project_folder(assets_dir: Path, project_id: int, kind: str = "image") -> Path:
    d = assets_dir / str(project_id) / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_sandbox_artifact_delete_removes_file(client, monkeypatch):
    """uploads/ file: node delete unlinks the file (true library delete)."""
    assets_dir = config_module.settings.assets_dir
    up = assets_dir / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    f = up / "aaaa1111-shot.png"
    f.write_bytes(b"png")

    graph = _ensure_sandbox_canvas(client)
    canvas_id = graph["canvas"]["id"]
    node_id = _create_artifact_node(canvas_id, str(f))

    r = client.delete(f"/api/canvas/{canvas_id}/nodes/{node_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["file_deleted"] is True
    assert not f.exists()
    conn = get_db(config_module.settings.db_path)
    try:
        assert (
            conn.execute("SELECT 1 FROM canvas_node WHERE id = ?", (node_id,)).fetchone()
            is None
        )
    finally:
        conn.close()


def test_sandbox_scratch_output_delete_removes_file(client):
    """Scratch-project output folders are deletable library files too."""
    assets_dir = config_module.settings.assets_dir
    # ensure scratch project exists with the system status marker
    conn = get_db(config_module.settings.db_path)
    try:
        conn.execute(
            "INSERT INTO projects (title, idea, status) VALUES ('Playground Scratch', NULL, 'system')"
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM projects WHERE status = 'system' LIMIT 1"
        ).fetchone()
        scratch_pid = int(row["id"])
    finally:
        conn.close()

    f = _project_folder(assets_dir, scratch_pid) / "job-1-image.png"
    f.write_bytes(b"png")

    graph = _ensure_sandbox_canvas(client)
    canvas_id = graph["canvas"]["id"]
    node_id = _create_artifact_node(canvas_id, str(f))

    r = client.delete(f"/api/canvas/{canvas_id}/nodes/{node_id}")
    assert r.status_code == 200
    assert r.json()["file_deleted"] is True
    assert not f.exists()


def test_project_owned_artifact_file_survives(client):
    """A node pointing at a real project's output: card removed, file kept."""
    pid = client.post("/api/projects", json={"title": "Alpha", "idea": "x"}).json()["id"]
    assets_dir = config_module.settings.assets_dir
    f = _project_folder(assets_dir, pid) / "job-9-image.png"
    f.write_bytes(b"png")

    graph = client.post("/api/canvas", json={"project_id": pid}).json()
    canvas_id = graph["canvas"]["id"]
    node_id = _create_artifact_node(canvas_id, str(f))

    r = client.delete(f"/api/canvas/{canvas_id}/nodes/{node_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["file_deleted"] is False
    assert body["reason"] == "project_owned"
    assert f.exists(), "project-owned file must never be deleted"


def test_referenced_file_survives(client):
    """File referenced by an entity row: node deleted, file kept."""
    pid = client.post("/api/projects", json={"title": "Beta", "idea": "x"}).json()["id"]
    _seed_story_entities(pid)
    assets_dir = config_module.settings.assets_dir
    up = assets_dir / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    f = up / "bbbb2222-portrait.png"
    f.write_bytes(b"png")

    conn = get_db(config_module.settings.db_path)
    try:
        conn.execute(
            "UPDATE characters SET portrait_path = ? WHERE project_id = ?",
            (str(f), pid),
        )
        conn.commit()
    finally:
        conn.close()

    graph = client.post("/api/canvas", json={"project_id": pid}).json()
    canvas_id = graph["canvas"]["id"]
    node_id = _create_artifact_node(canvas_id, str(f))

    r = client.delete(f"/api/canvas/{canvas_id}/nodes/{node_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["file_deleted"] is False
    assert body["reason"] == "referenced"
    assert f.exists()


def test_entity_node_delete_tombstones(client):
    """Entity nodes keep tombstone semantics (no resurrection by auto-seed)."""
    pid = client.post("/api/projects", json={"title": "Gamma", "idea": "x"}).json()["id"]
    _seed_story_entities(pid)
    graph = client.post("/api/canvas", json={"project_id": pid}).json()
    canvas_id = graph["canvas"]["id"]
    entity_node_id = next(
        n["id"] for n in graph["nodes"] if n["type"] == "entity" and n["entity_type"] == "character"
    )
    r = client.delete(f"/api/canvas/{canvas_id}/nodes/{entity_node_id}")
    assert r.status_code == 200
    assert r.json()["file_deleted"] is False
    conn = get_db(config_module.settings.db_path)
    try:
        row = conn.execute(
            "SELECT deleted FROM canvas_node WHERE id = ?", (entity_node_id,)
        ).fetchone()
        assert row is not None and row["deleted"] == 1
    finally:
        conn.close()


def test_delete_unknown_node_404(client):
    sid = client.post("/api/agent/sessions", json={}).json()["id"]
    g = client.post("/api/canvas", json={"agent_session_id": sid}).json()
    r = client.delete(f"/api/canvas/{g['canvas']['id']}/nodes/99999")
    assert r.status_code == 404


def test_delete_publishes_canvas_updated(client, monkeypatch):
    """Other tabs sync: node deletion publishes canvas.updated."""
    events: list[tuple[str, dict]] = []

    async def fake_publish(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    import calliope.events.bus as bus_mod

    monkeypatch.setattr(bus_mod.event_bus, "publish", fake_publish)

    graph = _ensure_sandbox_canvas(client)
    canvas_id = graph["canvas"]["id"]
    node_id = _create_artifact_node(canvas_id, "")
    r = client.delete(f"/api/canvas/{canvas_id}/nodes/{node_id}")
    assert r.status_code == 200
    updates = [d for (t, d) in events if t == "canvas.updated"]
    assert updates and updates[-1]["reason"] == "node_deleted"


def test_node_delete_keeps_file_referenced_by_other_canvas_node(client):
    """Two cards sharing one file: deleting one keeps the file (other card)."""
    assets_dir = config_module.settings.assets_dir
    up = assets_dir / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    f = up / "cccc3333-shared.png"
    f.write_bytes(b"png")

    graph = _ensure_sandbox_canvas(client)
    canvas_id = graph["canvas"]["id"]
    node_a = _create_artifact_node(canvas_id, str(f))
    node_b = _create_artifact_node(canvas_id, str(f))

    r = client.delete(f"/api/canvas/{canvas_id}/nodes/{node_a}")
    assert r.status_code == 200
    assert r.json()["file_deleted"] is False
    assert r.json()["reason"] == "referenced"
    assert f.exists(), "file still shown by the second card"
