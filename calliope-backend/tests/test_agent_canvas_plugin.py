"""Canvas harness plugin tests — the agent-side graph tooling.

These tests call the plugin's ``_db()`` helper directly (no API client), so
they must pin the harness settings to a scratch dir themselves — otherwise
every test run writes test rows (img2vid workflows, 'E' projects) into the
REAL data/calliope.db. The autouse fixture mirrors tests/conftest.py.
"""
from __future__ import annotations

import asyncio

import pytest

from calliope.agent.harness import build_harness
from calliope.agent.harness.plugins import canvas as canvas_plugin
from calliope.agent.harness.registry import ToolContext, _db


@pytest.fixture(autouse=True)
def _scratch_db(monkeypatch, tmp_path):
    """Point settings.data_dir at a per-test temp dir (never the real one);
    settings.db_path is a read-only property over data_dir."""
    import calliope.config as config_module

    monkeypatch.setattr(config_module.settings, "data_dir", tmp_path)
    from calliope.db import migrate_db

    asyncio.run(migrate_db(tmp_path / "calliope.db"))
    yield


@pytest.fixture()
def registry():
    reg, _prompts = build_harness()
    return reg


def _make_project(conn) -> int:
    cur = conn.execute("INSERT INTO projects (title, idea) VALUES ('E', 'x')")
    conn.commit()
    return int(cur.lastrowid)


def _make_canvas(conn, pid: int) -> int:
    cur = conn.execute(
        "INSERT INTO canvas (project_id, title) VALUES (?, 'Project canvas')", (pid,)
    )
    conn.commit()
    return int(cur.lastrowid)


def _make_workflow(conn, *, tagged: bool = True) -> int:
    wf_json = (
        # Role-tagged API-format node: title (Input:image) → role "image"
        '{"12": {"class_type": "LoadImage", "inputs": {}, "_meta": '
        '{"title": "Ref (Input:image)"}}}'
        if tagged
        else '{"9": {"class_type": "Loading", "inputs": {}}}'
    )
    cur = conn.execute(
        """
        INSERT INTO workflows (name, kind, workflow_json, input_schema, output_schema,
                               prompt_profile, is_enabled)
        VALUES ('img2vid', 'video', ?, '[]', '[]', 'prose', 1)
        """,
        (wf_json,),
    )
    conn.commit()
    return int(cur.lastrowid)


def _node(conn, canvas_id: int, **kw) -> int:
    payload = {
        "type": "workflow",
        "title": None,
        "workflow_id": None,
        "artifact_path": None,
        "status": "idle",
    }
    payload.update(kw)
    cur = conn.execute(
        """
        INSERT INTO canvas_node
            (canvas_id, type, title, x, y, workflow_id, artifact_path, status,
             input_values_json, created_at, updated_at)
        VALUES (?, ?, ?, 0, 0, ?, ?, ?, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (
            canvas_id,
            payload["type"],
            payload["title"],
            payload["workflow_id"],
            payload["artifact_path"],
            payload["status"],
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_canvas_tools_registered(registry):
    tools = registry.tools
    names = set(tools.keys())
    for expected in (
        "summarize_canvas",
        "create_canvas_node",
        "canvas_connect",
        "canvas_link",
        "update_canvas_node",
        "run_canvas_node",
        "post_artifact_to_canvas",
        "delete_canvas_node",
    ):
        assert expected in names, f"{expected} missing"
    assert tools["run_canvas_node"].requires_approval, "run_canvas_node must be HITL"


def test_canvas_tools_visible_in_sandbox(registry):
    """The free-flow sandbox contract: the agent must be able to see and post
    to its session's canvas WITHOUT a linked project. requires_project
    defaults to True, and a miss hides the tool from the sandbox payload —
    which is exactly how post_artifact_to_canvas 'disappeared' and the model
    started telling users to attach images to a film instead."""
    from calliope.agent.harness.tools import openai_tools_payload

    # Sandbox context (project_id=None) — session id arbitrary.
    sandbox_names = {
        entry["function"]["name"]
        for entry in openai_tools_payload(ToolContext(session_id=9_999_070, project_id=None))
    }
    for expected in (
        "summarize_canvas",
        "post_artifact_to_canvas",
        "create_canvas_node",
        "update_canvas_node",
    ):
        assert expected in sandbox_names, (
            f"{expected} hidden in sandbox — the free-flow canvas contract is broken"
        )


def test_run_canvas_node_blocked_without_user_intent(registry):
    """run_canvas_node is requires_approval: the render guard must deny a run
    when the user never asked for generation (mirrors run_workflow HITL)."""
    conn = _db()
    try:
        pid = _make_project(conn)
        cid = _make_canvas(conn, pid)
        node_id = _node(conn, cid, type="workflow", workflow_id=_make_workflow(conn))
    finally:
        conn.close()
    # Linked session, fresh event log → no render intent anywhere.
    ctx = ToolContext(session_id=9_999_042, project_id=pid)
    result = asyncio.run(registry.execute(ctx, "run_canvas_node", {"node_id": node_id}))
    assert result["ok"] is False
    assert "blocked" in result["error"]


def test_summarize_and_link_flow():
    """Project-linked session: summarize sees nodes, link edges, data edges."""
    conn = _db()
    try:
        pid = _make_project(conn)
        cid = _make_canvas(conn, pid)
        a = _node(conn, cid, type="image", artifact_path="C:/a/x.png", title="ref img")
        b = _node(conn, cid, type="workflow", workflow_id=_make_workflow(conn), title="vid wf")
    finally:
        conn.close()

    ctx = ToolContext(session_id=1, project_id=pid)

    link = asyncio.run(
        canvas_plugin.t_link_nodes(ctx, {"src_node_id": a, "dst_node_id": b, "label": "inspires"})
    )
    assert link["ok"], link
    connect = asyncio.run(
        canvas_plugin.t_connect(ctx, {"src_node_id": a, "dst_node_id": b, "dst_role": "image"})
    )
    assert connect["ok"], connect
    assert connect["dst_role"] == "image"

    summary = asyncio.run(canvas_plugin.t_summarize_canvas(ctx, {}))
    assert summary["ok"] and summary["canvas_id"] == cid
    assert summary["counts"]["links"] == 1
    assert summary["counts"]["data"] == 1
    assert summary["link_edges"][0]["label"] == "inspires"
    assert summary["data_edges"][0]["dst_role"] == "image"
    titles = {n["title"] for n in summary["nodes"]}
    assert {"ref img", "vid wf"} <= titles


def test_create_node_and_update_inputs():
    conn = _db()
    try:
        pid = _make_project(conn)
        _make_canvas(conn, pid)
        wid = _make_workflow(conn)
    finally:
        conn.close()
    ctx = ToolContext(session_id=1, project_id=pid)

    created = asyncio.run(
        canvas_plugin.t_create_node(ctx, {"type": "workflow", "workflow_id": wid})
    )
    assert created["ok"], created
    node_id = created["canvas_node_id"]

    updated = asyncio.run(
        canvas_plugin.t_update_node(
            ctx, {"node_id": node_id, "input_values": {"12": "hello"}}
        )
    )
    assert updated["ok"], updated
    # Second update merges, not replaces.
    updated2 = asyncio.run(
        canvas_plugin.t_update_node(
            ctx, {"node_id": node_id, "input_values": {"15": 512}}
        )
    )
    assert updated2["ok"]
    conn = _db()
    try:
        row = conn.execute(
            "SELECT input_values_json FROM canvas_node WHERE id = ?", (node_id,)
        ).fetchone()
        import json

        values = json.loads(row["input_values_json"])
        assert values == {"12": "hello", "15": 512}
    finally:
        conn.close()


def test_connect_rejects_unknown_role():
    conn = _db()
    try:
        pid = _make_project(conn)
        cid = _make_canvas(conn, pid)
        a = _node(conn, cid, type="image", artifact_path="C:/a/x.png")
        b = _node(conn, cid, type="workflow", workflow_id=_make_workflow(conn))
    finally:
        conn.close()
    ctx = ToolContext(session_id=1, project_id=pid)

    result = asyncio.run(
        canvas_plugin.t_connect(
            ctx,
            {
                "src_node_id": a,
                "dst_node_id": b,
                "dst_role": "warp",
            },
        )
    )
    assert not result["ok"]
    assert "no input with role" in result["error"]

    # Untagged workflow: no roles discoverable at all
    conn = _db()
    try:
        c2 = _make_canvas(conn, pid)
        plain_wf = _make_workflow(conn, tagged=False)
        a2 = _node(conn, c2, type="image", artifact_path="C:/a/y.png")
        b2 = _node(conn, c2, type="workflow", workflow_id=plain_wf)
    finally:
        conn.close()
    ctx2 = ToolContext(session_id=1, project_id=pid)
    result2 = asyncio.run(
        canvas_plugin.t_connect(ctx2, {"src_node_id": a2, "dst_node_id": b2, "dst_role": "image"})
    )
    assert not result2["ok"]
    assert "Available roles" in result2["error"]


def test_delete_node_tombstones_entities_and_drops_edges():
    conn = _db()
    try:
        pid = _make_project(conn)
        cid = _make_canvas(conn, pid)
        ent = _node(conn, cid, type="entity", title="Kira")
        wf = _node(conn, cid, type="workflow", workflow_id=_make_workflow(conn))
        conn.execute(
            """
            INSERT INTO canvas_edge (canvas_id, src_node_id, dst_node_id, kind, dst_role,
                                     dst_comfy_node_id, created_at)
            VALUES (?, ?, ?, 'data', 'image', '12', CURRENT_TIMESTAMP)
            """,
            (cid, ent, wf),
        )
        conn.commit()
    finally:
        conn.close()
    ctx = ToolContext(session_id=1, project_id=pid)

    result = asyncio.run(canvas_plugin.t_delete_node(ctx, {"node_id": ent}))
    assert result["ok"]
    conn = _db()
    try:
        row = conn.execute("SELECT deleted FROM canvas_node WHERE id = ?", (ent,)).fetchone()
        assert row["deleted"] == 1, "entity nodes tombstone"
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM canvas_edge WHERE dst_node_id = ?", (ent,)
        ).fetchone()["c"]
        assert n == 0, "attached edges dropped"
    finally:
        conn.close()


def _make_session(conn, project_id: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO agent_sessions (project_id, title) VALUES (?, 'S')",
        (project_id,),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_sandbox_canvas_get_or_create_and_post():
    """A sandbox session posting an artifact gets its own canvas created on
    demand — posting must never dead-end on 'no canvas yet' and push the
    model toward attaching to a real film (the free-flow sandbox contract)."""
    conn = _db()
    try:
        sid = _make_session(conn)
    finally:
        conn.close()
    ctx = ToolContext(session_id=sid, project_id=None)

    # No canvas yet → get-or-create makes one
    cid1, err1 = canvas_plugin._canvas_for_session(ctx)
    assert err1 is None and cid1 is not None
    # Idempotent: the same canvas comes back on the next call
    cid2, err2 = canvas_plugin._canvas_for_session(ctx)
    assert err2 is None and cid2 == cid1

    # post_artifact_to_canvas works with no project at all
    posted = asyncio.run(
        canvas_plugin.t_post_artifact(ctx, {"asset_path": "C:/a/gen.png", "title": "ninja"})
    )
    assert posted["ok"], posted
    assert posted["canvas_node_id"] > 0

    # summarize sees the sandbox board and its card
    summary = asyncio.run(canvas_plugin.t_summarize_canvas(ctx, {}))
    assert summary["ok"] and summary["canvas_id"] == cid1
    assert any(n["title"] == "ninja" for n in summary["nodes"])


def test_post_artifact_dedupes_per_job():
    """The agent's post_artifact_to_canvas and the frontend auto-materializer
    both write artifact nodes for a finished job. Two cards for one output
    (observed live on canvas/65) is the bug this guards: the tool dedupes,
    the unique index backs it, and the API returns the winner on a race."""
    conn = _db()
    try:
        sid = _make_session(conn)
        pid = _make_project(conn)
        cur = conn.execute(
            "INSERT INTO jobs (project_id, kind, status, payload_json, output_paths_json, created_at) "
            "VALUES (?, 'image', 'done', '{}', '[\"C:/a/dancer.png\"]', CURRENT_TIMESTAMP)",
            (pid,),
        )
        job_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    ctx = ToolContext(session_id=sid, project_id=None)

    # Agent posts the artifact
    first = asyncio.run(
        canvas_plugin.t_post_artifact(ctx, {"job_id": job_id, "title": "dancer"})
    )
    assert first["ok"], first
    assert not first.get("deduplicated")
    summary = asyncio.run(canvas_plugin.t_summarize_canvas(ctx, {}))
    canvas_id = summary["canvas_id"]

    # The SSE materializer posts the SAME job via the API (its own check
    # raced and lost) → the API returns the existing node instead of a twin.
    # Simulated by a raw INSERT that violates uq_canvas_node_job → caught
    # by the router's IntegrityError → existing-node path. Here we assert
    # the unique index itself rejects the twin.
    import sqlite3 as _sq

    dup_insert = None
    conn = _db()
    try:
        x, y = 1100.0, 80.0
        conn.execute(
            """
            INSERT INTO canvas_node
                (canvas_id, type, title, x, y, artifact_path, job_id, status,
                 created_at, updated_at)
            VALUES (?, 'image', 'twin', ?, ?, 'C:/a/dancer.png', ?, 'done',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (canvas_id, x, y, job_id),
        )
        conn.commit()
    except _sq.IntegrityError as exc:
        dup_insert = str(exc)
    finally:
        conn.close()
    assert dup_insert is not None and "UNIQUE constraint failed" in dup_insert, (
        f"unique index did not reject the twin: {dup_insert!r}"
    )

    # Direct tool re-post: deduplicated receipt, no second card
    again = asyncio.run(canvas_plugin.t_post_artifact(ctx, {"job_id": job_id, "title": "dancer"}))
    assert again["ok"] is True and again.get("deduplicated") is True
    assert again["canvas_node_id"] == first["canvas_node_id"]

    # Count artifact nodes for this job on this canvas — must be exactly 1
    conn = _db()
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM canvas_node WHERE canvas_id = ? AND job_id = ? "
            "AND type IN ('image','video') AND deleted = 0",
            (canvas_id, job_id),
        ).fetchone()["c"]
    finally:
        conn.close()
    assert n == 1, f"expected 1 artifact node for job {job_id}, found {n}"
