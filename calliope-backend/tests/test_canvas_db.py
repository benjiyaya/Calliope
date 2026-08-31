"""Canvas v2.1 Phase 1 — canvas/canvas_node tables, scoping, and cascades."""
from __future__ import annotations

import sqlite3


def _get_canvas(client, canvas_id: int):
    r = client.get(f"/api/canvas/{canvas_id}")
    assert r.status_code == 200
    return r.json()


def test_canvas_tables_exist(client):
    """Both tables must exist post-migration with the agreed columns."""
    import calliope.config as config_module
    from calliope.db import get_db

    conn = get_db(config_module.settings.db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(canvas)").fetchall()}
        assert {"id", "project_id", "agent_session_id", "title", "viewport_json"} <= cols
        node_cols = {r[1] for r in conn.execute("PRAGMA table_info(canvas_node)").fetchall()}
        assert {
            "id",
            "canvas_id",
            "type",
            "x",
            "y",
            "artifact_path",
            "entity_type",
            "entity_id",
        } <= node_cols
    finally:
        conn.close()


def test_project_delete_cascades_canvas(client):
    pid = client.post("/api/projects", json={"title": "C1", "idea": "x"}).json()["id"]
    r = client.post("/api/canvas", json={"project_id": pid, "title": "Bible"})
    assert r.status_code == 200
    canvas_id = r.json()["canvas"]["id"]

    client.delete(f"/api/projects/{pid}")

    rr = client.get(f"/api/canvas/{canvas_id}")
    assert rr.status_code == 404


def test_session_delete_cascades_sandbox_canvas(client):
    sid = client.post("/api/agent/sessions", json={}).json()["id"]
    r = client.post("/api/canvas", json={"agent_session_id": sid})
    assert r.status_code == 200
    canvas_id = r.json()["canvas"]["id"]

    client.delete(f"/api/agent/sessions/{sid}")

    rr = client.get(f"/api/canvas/{canvas_id}")
    assert rr.status_code == 404


def test_artifact_path_rebases_on_move(client):
    """canvas_node.artifact_path must be registered in _PATH_COLUMNS so a
    folder move rebases stored absolute paths (the migration runs at startup)."""
    from calliope.db import _PATH_COLUMNS

    assert "canvas_node" in _PATH_COLUMNS
    assert "artifact_path" in _PATH_COLUMNS["canvas_node"]
