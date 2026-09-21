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


def test_story_role_can_structure_beats_into_scenes():
    """Session 908 regression: the planner scheduled 'create 8 scenes via
    add_scene' as a STORY task, but add_scene lived only in the script role —
    the story agent reported 'Tool not available to this role: add_scene',
    scenes were never created, and the build stalled with nothing in the
    project DB. Scene-authoring basics belong to the story role too."""
    tools = orchestrator.ROLE_TOOLS["story"]
    assert "add_scene" in tools
    assert "list_scenes" in tools


def test_sub_agent_payload_keeps_render_tools_visible():
    """Session 908 regression: a planner-scheduled video render task must see
    enqueue_video_jobs in its payload even without prose render intent — the
    scoped payload filters requires_project/blind_only/scene-scope only, and
    PERMISSION is enforced at execute time (guard denial teaches ask_user).
    The old _visible()-based filter hid the tools entirely, so the sub-agent
    ended with 'the generation tool is not exposed in this session's
    toolset' and the render never happened."""

    class _Recorder:
        def __init__(self) -> None:
            self.names: list[str] = []

        def __call__(self, fn):
            self.names.append(fn.__name__)
            return fn

    import calliope.agent.harness.orchestrator as orch

    # No user/message events at all → user_allows_render is False, so the
    # old payload filter would have dropped every requires_approval tool.
    payload = orch._scoped_payload(
        ToolContext(session_id=999_001, project_id=1),
        orchestrator.ROLE_TOOLS["video"],
    )
    names = {p["function"]["name"] for p in payload}
    assert "enqueue_video_jobs" in names
    assert "run_workflow" in names
    # Sanity: truly project-scoped tools still filter for blind contexts.
    blind = orch._scoped_payload(
        ToolContext(session_id=999_001, project_id=None),
        orchestrator.ROLE_TOOLS["video"],
    )
    blind_names = {p["function"]["name"] for p in blind}
    assert "enqueue_video_jobs" not in blind_names
    assert "get_workspace" in blind_names  # requires_project=False


def test_execute_time_render_guard_still_blocks_without_intent():
    """The payload fix must not weaken the execute-time gate: with no render
    intent, calling enqueue_video_jobs still returns the HITL denial (which
    instructs ask_user) rather than executing."""
    import json as _json

    from calliope.agent.harness import build_harness, get_registry

    build_harness()
    registry = get_registry()
    # Session 999_002 has no events → no render permission.
    result = asyncio.run(
        registry.execute(
            ToolContext(session_id=999_002, project_id=1),
            "enqueue_video_jobs",
            {"scene_ids": [1]},
        )
    )
    assert result.get("ok") is False
    assert result.get("reason_code") == "guard_render_approval"
    assert "ask" in _json.dumps(result).lower()


def test_swarm_roles_cover_pipeline_categories():
    """Drift guard (both directions): every registered tool in a pipeline
    category must appear in its ROLE_TOOLS role — the clip tools drifted out
    of the script/video roles while the dead-name check above stayed green,
    because nothing checked the orphan direction.

    Deliberately role-less categories are excluded: canvas / interaction /
    memory / skills / shot / workspace / project / system are main-loop-only
    or shared base reads.
    """
    from calliope.agent.harness import build_harness

    registry, _ = build_harness()
    category_roles = {
        "story": {"story"},
        "script": {"script"},
        "assets": {"assets"},
        "video": {"video"},
    }
    # Story-category ENTITY CRUD is planner-routed to the assets role (the
    # standard EDIT pipeline: story → script → "add/update assets text") —
    # only beats stay with the story role.
    overrides = {
        name: {"assets"}
        for name in (
            "add_character",
            "update_character",
            "delete_character",
            "add_location",
            "update_location",
            "delete_location",
            "add_item",
            "update_item",
            "delete_item",
        )
    }
    for t in registry.tools.values():
        roles = overrides.get(t.name) or category_roles.get(t.category)
        if not roles:
            continue
        for role in roles:
            assert t.name in orchestrator.ROLE_TOOLS[role], (
                f"{t.category}-category tool '{t.name}' is missing from the "
                f"'{role}' swarm role — sub-agents cannot perform it"
            )


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
