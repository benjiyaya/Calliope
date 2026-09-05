"""Regression: the mechanical permission/scope contract.

Permission (may I generate at all?) comes from explicit user acts:
ask_user card, @workflow tag, render verbs, terse confirmation.
Scope (which entities?) is enforced mechanically by the enqueue tools:
explicit ids, or all_missing=true, or an "all/remaining" word when the
unscoped call would touch more than 3 entities.

The canvas/65 incident in both directions:
- old prose-scope heuristics denied a genuine "2 characters" request,
- then an unscoped enqueue regenerated EVERY entity.
Both are now impossible by construction.
"""
from __future__ import annotations

import asyncio

import pytest

from calliope.agent.harness import log as session_log
from calliope.agent.harness import orchestrator
from calliope.agent.harness.policy import user_allows_render
from calliope.agent.harness.registry import ToolContext
from calliope.agent.harness.tools import openai_tools_payload


@pytest.fixture(autouse=True)
def _scratch_db(monkeypatch, tmp_path):
    import calliope.config as config_module

    monkeypatch.setattr(config_module.settings, "data_dir", tmp_path)
    from calliope.db import get_db, migrate_db

    asyncio.run(migrate_db(tmp_path / "calliope.db"))
    conn = get_db(tmp_path / "calliope.db")
    conn.execute("INSERT INTO projects (id, title) VALUES (1, 'Tag Film')")
    conn.execute("INSERT INTO agent_sessions (id, title) VALUES (1, 'tagged')")
    conn.execute("INSERT INTO agent_sessions (id, title) VALUES (2, 'untagged')")
    conn.commit()
    conn.close()
    yield


def test_workflow_tag_grants_render_permission():
    """Tagging @workflow (a deliberate picker act) grants render permission
    even when the prose has no render verbs — the canvas/65 case."""
    session_log.append_event(
        1,
        session_log.USER_MESSAGE,
        {
            "content": "use this workflow for character Lord Kael and Lila",
            "mentions": [
                {"type": "workflow", "id": 35, "name": "krea2_CharSheet", "kind": "image"}
            ],
        },
    )
    ctx = ToolContext(session_id=1, project_id=1)
    assert user_allows_render(ctx) is True
    names = {e["function"]["name"] for e in openai_tools_payload(ctx)}
    assert "run_workflow" in names
    assert "enqueue_asset_jobs" in names


def test_incidental_prose_still_denied():
    """No tag + no render verbs → no permission (the original protection)."""
    session_log.append_event(
        2,
        session_log.USER_MESSAGE,
        {"content": "create a Misc. Item called Flame Tokens"},
    )
    ctx = ToolContext(session_id=2, project_id=1)
    assert user_allows_render(ctx) is False
    names = {e["function"]["name"] for e in openai_tools_payload(ctx)}
    assert "run_workflow" not in names


def test_assets_role_scope_includes_canvas_posting():
    """The swarm assets sub-agent must be able to post artifacts — the
    canvas/65 sub-agent wrote 'an orchestrator needs to fire this' because
    its scope ended at wait_for_jobs."""
    assert "post_artifact_to_canvas" in orchestrator.ROLE_TOOLS["assets"]
    from calliope.agent.harness import build_harness

    registry, _ = build_harness()
    for role, tools in orchestrator.ROLE_TOOLS.items():
        for name in tools:
            assert registry.get(name) is not None, f"{role} names unknown tool {name}"


def test_unscoped_enqueue_refused_when_many_missing():
    """Mechanical scope: an unscoped enqueue_asset_jobs that would touch
    more than 3 entities is refused with the count, so '2 characters' can
    never become 'regenerate everything'."""
    from calliope.agent.harness import log as session_log
    from calliope.agent.harness.plugins.render import t_enqueue_asset_jobs
    from calliope.agent.harness.registry import ToolContext, _db

    conn = _db()
    try:
        pid = 1
        for i in range(5):
            conn.execute(
                "INSERT INTO characters (project_id, name) VALUES (?, ?)",
                (pid, f"Char{i}"),
            )
        conn.commit()
    finally:
        conn.close()

    session_log.append_event(
        1,
        session_log.USER_MESSAGE,
        {
            "content": "generate images for Kael and Lila",
            "mentions": [{"type": "workflow", "id": 1, "name": "wf", "kind": "image"}],
        },
    )
    ctx = ToolContext(session_id=1, project_id=pid)
    result = asyncio.run(t_enqueue_asset_jobs(ctx, {}))
    assert result["ok"] is False, result
    assert result["would_hit"] >= 5
    assert "explicit" in result["error"]


def test_scoped_enqueue_allowed_for_two():
    """Explicit ids for the two named characters enqueue exactly those two."""
    from calliope.agent.harness import log as session_log
    from calliope.agent.harness.plugins.render import t_enqueue_asset_jobs
    from calliope.agent.harness.registry import ToolContext, _db

    conn = _db()
    try:
        pid = 1
        ids = []
        for name in ("Kael", "Lila"):
            cur = conn.execute(
                "INSERT INTO characters (project_id, name) VALUES (?, ?)", (pid, name)
            )
            ids.append(int(cur.lastrowid))
        conn.commit()
    finally:
        conn.close()

    session_log.append_event(
        1,
        session_log.USER_MESSAGE,
        {
            "content": "generate images for Kael and Lila",
            "mentions": [{"type": "workflow", "id": 1, "name": "wf", "kind": "image"}],
        },
    )
    ctx = ToolContext(session_id=1, project_id=pid)
    # No workflow row exists → the tool proceeds past the scope gate and
    # fails on workflow resolution, which proves the scope gate passed.
    result = asyncio.run(t_enqueue_asset_jobs(ctx, {"character_ids": ids}))
    assert "would_hit" not in result, result
