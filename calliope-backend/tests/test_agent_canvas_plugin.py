"""Canvas tools: sandbox-visible CRUD over canvas_node/canvas_edge, with the
review gate on run_canvas_node (it never queues a job)."""

from __future__ import annotations

import asyncio

import pytest

from calliope.agent.capabilities import canvas_ops, catalog
from calliope.config import settings
from calliope.db import get_db, migrate_db


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "assets_dir", tmp_path / "assets")
    asyncio.run(migrate_db(settings.db_path))
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute("INSERT INTO projects (title) VALUES ('Board')")
        project_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    return project_id


def _mk_session() -> int:
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute("INSERT INTO agent_sessions (title) VALUES ('chat')")
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def test_sandbox_canvas_get_or_created_by_session(db):
    sid = _mk_session()
    # Sandbox (project None) canvas tools work — the canvas is keyed by the session.
    out = asyncio.run(catalog.execute("create_canvas_node", {"type": "text", "title": "note"}, None, sid))
    assert out["ok"] is True
    summary = canvas_ops.summarize_canvas(None, sid)
    assert summary["ok"] is True
    assert summary["canvas_id"]
    assert [n["title"] for n in summary["nodes"]] == ["note"]


def test_project_canvas_get_or_create_is_idempotent(db):
    pid = db
    canvas_ops.canvas_for(pid, None)
    again = canvas_ops.canvas_for(pid, None)
    conn = get_db(settings.db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM canvas WHERE project_id = ?", (pid,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1 and again["project_id"] == pid


def test_node_crud(db):
    sid = _mk_session()
    node = asyncio.run(
        catalog.execute("create_canvas_node", {"type": "text", "title": "Idea", "x": 10, "y": 20}, None, sid)
    )
    node_id = node["node_id"]
    moved = asyncio.run(
        catalog.execute("update_canvas_node", {"node_id": node_id, "x": 99, "y": 88, "title": "Renamed"}, None, sid)
    )
    assert moved["ok"] is True
    summary = canvas_ops.summarize_canvas(None, sid)
    found = next(n for n in summary["nodes"] if n["id"] == node_id)
    assert found["x"] == 99 and found["title"] == "Renamed"
    deleted = asyncio.run(
        catalog.execute("delete_canvas_node", {"node_id": node_id}, None, sid)
    )
    assert deleted["ok"] is True
    summary = canvas_ops.summarize_canvas(None, sid)
    assert all(n["id"] != node_id for n in summary["nodes"])


def test_data_edge_validation(db):
    pid = db
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO workflows (name, kind, workflow_json) VALUES ('img', 'image', '{}')"
        )
        workflow_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    node = asyncio.run(
        catalog.execute(
            "create_canvas_node",
            {"type": "workflow", "title": "Render", "workflow_id": workflow_id},
            pid,
            None,
        )
    )
    dst = node["node_id"]
    # No source edge → invalid (dst needs a source with media).
    missing = asyncio.run(
        catalog.execute(
            "canvas_connect", {"src_node_id": dst, "dst_node_id": dst, "dst_role": "prompt"}, pid, None
        )
    )
    assert missing["ok"] is False
    # A source artifact node makes the edge valid.
    src = asyncio.run(
        catalog.execute(
            "create_canvas_node",
            {"type": "image", "title": "ref", "artifact_path": "C:/x.png"},
            pid,
            None,
        )
    )
    edge = asyncio.run(
        catalog.execute(
            "canvas_connect",
            {"src_node_id": src["node_id"], "dst_node_id": dst, "dst_role": "prompt"},
            pid,
            None,
        )
    )
    assert edge["ok"] is True
    dup = asyncio.run(
        catalog.execute(
            "canvas_connect",
            {"src_node_id": src["node_id"], "dst_node_id": dst, "dst_role": "prompt"},
            pid,
            None,
        )
    )
    assert dup["ok"] is False


def test_link_edge_needs_label(db):
    pid = db
    a = asyncio.run(catalog.execute("create_canvas_node", {"type": "text", "title": "A"}, pid, None))
    b = asyncio.run(catalog.execute("create_canvas_node", {"type": "text", "title": "B"}, pid, None))
    missing = asyncio.run(
        catalog.execute(
            "canvas_link", {"src_node_id": a["node_id"], "dst_node_id": b["node_id"]}, pid, None
        )
    )
    assert missing["ok"] is False
    ok = asyncio.run(
        catalog.execute(
            "canvas_link",
            {"src_node_id": a["node_id"], "dst_node_id": b["node_id"], "label": "same actor"},
            pid,
            None,
        )
    )
    assert ok["ok"] is True


def test_run_canvas_node_reviews_and_never_queues(db):
    pid = db
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO workflows (name, kind, workflow_json) VALUES ('vid', 'video', '{}')"
        )
        workflow_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    node = asyncio.run(
        catalog.execute(
            "create_canvas_node",
            {"type": "workflow", "title": "Render", "workflow_id": workflow_id},
            pid,
            None,
        )
    )
    result = asyncio.run(
        catalog.execute("run_canvas_node", {"node_id": node["node_id"]}, pid, None)
    )
    assert result["ok"] is True
    assert result.get("awaiting_review") is True
    # Nothing was queued.
    conn = get_db(settings.db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_project_tools_refused_unlinked_but_canvas_ok(db):
    sid = _mk_session()
    refused = asyncio.run(catalog.execute("add_scene", {"heading": "X"}, None, sid))
    assert refused["ok"] is False
    assert "not linked" in refused["error"]
    allowed = asyncio.run(
        catalog.execute("create_canvas_node", {"type": "text", "title": "free"}, None, sid)
    )
    assert allowed["ok"] is True
