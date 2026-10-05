"""Scene → Clips: schema backfill, default-clip invariant, CRUD, coverage contract.

These tests pin the 1 scene : N clips model — the renderable unit is a CLIP,
never the script scene itself. Real DB never touched (client fixture is
temp-scoped per the clean-test-artifacts rule).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from calliope.agent.coverage_agent import _normalize_clips, expand_scene_coverage
from calliope.agent.video_agent import (
    _clip_prompt_hash,
    _fetch_clips,
    enqueue_video_jobs,
)
from calliope.config import settings
from calliope.db import get_db


# ── helpers ─────────────────────────────────────────────────────────────


def _mk_project(client, title: str = "clip-project") -> int:
    return client.post("/api/projects", json={"title": title}).json()["id"]


def _add_scene(client, pid: int, order: int, **extra) -> dict:
    payload = {"order_index": order, "heading": f"S{order}", **extra}
    return client.post(f"/api/projects/{pid}/scenes", json=payload).json()


def _insert_workflow(client, conn, name: str, wf_json: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO workflows (name, kind, workflow_json, input_schema, output_schema, is_enabled)
        VALUES (?, 'video', ?, '{}', '{}', 1)
        """,
        (name, json.dumps(wf_json)),
    )
    conn.commit()
    return cur.lastrowid


def _scene_default_clip_id(conn, scene_id: int) -> int:
    row = conn.execute(
        "SELECT id FROM clips WHERE scene_id = ? ORDER BY order_index, id LIMIT 1",
        (scene_id,),
    ).fetchone()
    assert row is not None, "scene must always have >= 1 clip"
    return int(row["id"])


@pytest.fixture()
def video_workflow(client):
    conn = get_db(settings.db_path)
    try:
        wid = _insert_workflow(
            client,
            conn,
            "test-video",
            {
                "10": {
                    "class_type": "CLIPTextEncode",
                    "inputs": {"text": ""},
                    "_meta": {"title": "Main Prompt (Input:prompt)"},
                },
            },
        )
        return int(wid)
    finally:
        conn.close()


# ── schema + default-clip invariant ─────────────────────────────────────


def test_new_scene_gets_default_clip(client):
    pid = _mk_project(client)
    scene = _add_scene(client, pid, 1, duration_sec=9, dialog="ANNA\nHello.")
    assert len(scene["clips"]) == 1
    clip = scene["clips"][0]
    assert clip["order_index"] == 1
    assert clip["duration_sec"] == 9


def test_scene_update_ensures_default_clip(client):
    pid = _mk_project(client)
    scene = _add_scene(client, pid, 1)
    conn = get_db(settings.db_path)
    try:
        conn.execute("DELETE FROM clips WHERE scene_id = ?", (scene["id"],))
        conn.commit()
    finally:
        conn.close()
    r = client.patch(f"/api/projects/{pid}/scenes/{scene['id']}", json={"heading": "NEW"})
    assert r.status_code == 200
    assert len(r.json()["clips"]) == 1


def test_delete_last_clip_of_scene_rejected(client):
    pid = _mk_project(client)
    scene = _add_scene(client, pid, 1)
    clip_id = scene["clips"][0]["id"]
    r = client.delete(f"/api/projects/{pid}/clips/{clip_id}")
    assert r.status_code == 422
    assert "at least one clip" in r.json()["detail"]


def test_clip_crud_roundtrip(client):
    pid = _mk_project(client)
    scene = _add_scene(client, pid, 1)
    scene_id = scene["id"]
    r = client.post(
        f"/api/projects/{pid}/scenes/{scene_id}/clips",
        json={
            "description": "Close-up of hands",
            "shot_size": "closeUp",
            "dialog_lines_covered": [1, 2],
            "duration_sec": 6,
        },
    )
    assert r.status_code == 200
    clip = r.json()
    assert clip["order_index"] == 2
    assert clip["dialog_lines_covered"] == [1, 2]

    r = client.patch(
        f"/api/projects/{pid}/clips/{clip['id']}",
        json={"description": "Extreme close-up", "duration_sec": 5},
    )
    assert r.status_code == 200
    assert r.json()["description"] == "Extreme close-up"

    r = client.post(
        f"/api/projects/{pid}/scenes/{scene_id}/clips/reorder",
        json={"clip_ids": [clip["id"], scene["clips"][0]["id"]]},
    )
    assert r.status_code == 200
    orders = [c["order_index"] for c in r.json()["clips"]]
    assert orders == [1, 2]

    r = client.delete(f"/api/projects/{pid}/clips/{clip['id']}")
    assert r.status_code == 200
    r = client.get(f"/api/projects/{pid}/scenes").json()
    assert [c["id"] for c in r["scenes"][0]["clips"]] == [scene["clips"][0]["id"]]


# ── enqueue re-scope: per-clip jobs ─────────────────────────────────────


def test_enqueue_scene_expands_to_all_clips(client, video_workflow):
    pid = _mk_project(client)
    scene = _add_scene(client, pid, 1, dialog="ANNA\nOne.\nBOB\nTwo.")
    scene_id = scene["id"]
    conn = get_db(settings.db_path)
    try:
        for desc in ("wide", "medium"):
            conn.execute(
                "INSERT INTO clips (scene_id, project_id, order_index, description) VALUES (?, ?, ?, ?)",
                (scene_id, pid, 0, desc),
            )
        conn.execute("UPDATE clips SET order_index = order_index + 1 WHERE scene_id = ?", (scene_id,))
        conn.commit()
        n_clips = conn.execute(
            "SELECT COUNT(*) AS n FROM clips WHERE scene_id = ?", (scene_id,)
        ).fetchone()["n"]
    finally:
        conn.close()
    assert n_clips == 3

    jobs = asyncio.run(enqueue_video_jobs(pid, scene_ids=[scene_id]))
    assert len(jobs) == 3
    conn = get_db(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT clip_id FROM jobs WHERE id IN (%s)" % ",".join("?" * len(jobs)),
            [j["id"] for j in jobs],
        ).fetchall()
        clip_ids = {r["clip_id"] for r in rows}
        all_clip_ids = {
            r["id"]
            for r in conn.execute("SELECT id FROM clips WHERE scene_id = ?", (scene_id,)).fetchall()
        }
        assert clip_ids == all_clip_ids
    finally:
        conn.close()


def test_enqueue_by_clip_ids_renders_only_those(client, video_workflow):
    pid = _mk_project(client)
    scene = _add_scene(client, pid, 1)
    scene_id = scene["id"]
    conn = get_db(settings.db_path)
    try:
        for _ in range(2):
            conn.execute(
                "INSERT INTO clips (scene_id, project_id, order_index, description) VALUES (?, ?, ?, ?)",
                (scene_id, pid, 0, "x"),
            )
        conn.execute("UPDATE clips SET order_index = order_index + 1 WHERE scene_id = ?", (scene_id,))
        conn.commit()
        ids = [
            int(r["id"])
            for r in conn.execute(
                "SELECT id FROM clips WHERE scene_id = ? ORDER BY order_index", (scene_id,)
            ).fetchall()
        ]
    finally:
        conn.close()
    jobs = asyncio.run(enqueue_video_jobs(pid, clip_ids=ids[:2]))
    assert len(jobs) == 2
    conn = get_db(settings.db_path)
    try:
        got = {
            int(r["clip_id"])
            for r in conn.execute(
                "SELECT clip_id FROM jobs WHERE id IN (%s)" % ",".join("?" * len(jobs)),
                [j["id"] for j in jobs],
            ).fetchall()
        }
    finally:
        conn.close()
    assert got == set(ids[:2])


def test_supersede_and_clear_are_per_clip(client, video_workflow):
    pid = _mk_project(client)
    scene = _add_scene(client, pid, 1)
    scene_id = scene["id"]
    conn = get_db(settings.db_path)
    try:
        conn.execute(
            "INSERT INTO clips (scene_id, project_id, order_index, description) VALUES (?, ?, 2, 'second')",
            (scene_id, pid),
        )
        first = _scene_default_clip_id(conn, scene_id)
        second = conn.execute(
            "SELECT id FROM clips WHERE scene_id = ? AND order_index = 2", (scene_id,)
        ).fetchone()["id"]
        conn.execute(
            "UPDATE clips SET clip_path = '/x/first.mp4' WHERE id = ?", (first,)
        )
        conn.commit()
    finally:
        conn.close()
    jobs = asyncio.run(enqueue_video_jobs(pid, clip_ids=[int(second)]))
    assert len(jobs) == 1
    conn = get_db(settings.db_path)
    try:
        untouched = conn.execute(
            "SELECT clip_path FROM clips WHERE id = ?", (first,)
        ).fetchone()["clip_path"]
        cleared = conn.execute(
            "SELECT clip_path FROM clips WHERE id = ?", (second,)
        ).fetchone()["clip_path"]
    finally:
        conn.close()
    assert untouched == "/x/first.mp4", "re-enqueue must clear ONLY the target clip"
    assert cleared is None


# ── coverage expansion contract ─────────────────────────────────────────


def test_normalize_clips_distributes_missing_dialog_lines():
    clips = _normalize_clips(
        [
            {"description": "wide", "dialog_lines_covered": [1], "duration_sec": 6},
            {"description": "medium", "dialog_lines_covered": [], "duration_sec": 6},
        ],
        n_dialog_lines=3,
        scene_budget=12,
        clip_cap=8,
    )
    all_covered = sorted(n for c in clips for n in c["dialog_lines_covered"])
    assert all_covered == [1, 2, 3], "uncovered lines attach to the last clip"
    assert all(c["duration_sec"] <= 8 for c in clips)


def test_normalize_clips_reindexes_and_clamps_durations():
    clips = _normalize_clips(
        [
            {"order_index": 5, "description": "a", "duration_sec": 40},
            {"order_index": 9, "description": "b", "duration_sec": 3},
        ],
        n_dialog_lines=0,
        scene_budget=8,
        clip_cap=8,
    )
    assert [c["order_index"] for c in clips] == [1, 2]
    assert all(c["duration_sec"] <= 8 for c in clips)
    assert sum(c["duration_sec"] for c in clips) == 8


def test_expand_scene_coverage_replaces_clips(client, monkeypatch):
    pid = _mk_project(client)
    scene = _add_scene(
        client, pid, 1, dialog="ANNA\nHello.\nBOB\nHi.", duration_sec=12
    )
    scene_id = scene["id"]

    async def fake_llm(messages, temperature=0.5):
        return {
            "clips": [
                {
                    "order_index": 1,
                    "description": "Establishing wide",
                    "dialog_lines_covered": [1, 2],
                    "shot_size": "wide",
                    "duration_sec": 6,
                },
                {
                    "order_index": 2,
                    "description": "Reverse on BOB",
                    "dialog_lines_covered": [3, 4],
                    "shot_size": "medium",
                    "duration_sec": 6,
                },
            ]
        }

    monkeypatch.setattr("calliope.agent.coverage_agent.generate_structured", fake_llm)
    result = asyncio.run(expand_scene_coverage(pid, [scene_id]))
    assert result["scenes"][0]["clips"] == 2

    conn = get_db(settings.db_path)
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM clips WHERE scene_id = ? ORDER BY order_index", (scene_id,)
            ).fetchall()
        ]
        assert len(rows) == 2
        assert json.loads(rows[0]["dialog_lines_covered"]) == [1, 2]
        assert json.loads(rows[1]["dialog_lines_covered"]) == [3, 4]
        # Legacy scene mirror cleared after expansion
        scene_row = conn.execute(
            "SELECT video_path FROM scenes WHERE id = ?", (scene_id,)
        ).fetchone()
        assert scene_row["video_path"] is None
    finally:
        conn.close()


def test_expand_all_scenes_none_ids(client, monkeypatch):
    pid = _mk_project(client)
    _add_scene(client, pid, 1, action="A")
    _add_scene(client, pid, 2, action="B")

    async def fake_llm(messages, temperature=0.5):
        return {
            "clips": [
                {"description": "shot", "dialog_lines_covered": [], "duration_sec": 5},
            ]
        }

    monkeypatch.setattr("calliope.agent.coverage_agent.generate_structured", fake_llm)
    result = asyncio.run(expand_scene_coverage(pid, None))
    assert len(result["scenes"]) == 2


def test_expand_no_scenes_raises(client):
    pid = _mk_project(client)
    with pytest.raises(ValueError):
        asyncio.run(expand_scene_coverage(pid, []))


def test_expand_all_endpoint_honors_scene_ids(client, monkeypatch):
    """Per-scene 'Break Into Shots' must touch ONLY that scene.

    Regression: the project-level POST /expand-clips endpoint ignored
    body.scene_ids and always expanded every scene in the project.
    """
    pid = _mk_project(client)
    s1 = _add_scene(client, pid, 1, action="Scene one action.")
    s2 = _add_scene(client, pid, 2, action="Scene two action.")

    async def fake_llm(messages, temperature=0.5):
        return {
            "clips": [
                {"description": "shot A", "dialog_lines_covered": [], "duration_sec": 5},
                {"description": "shot B", "dialog_lines_covered": [], "duration_sec": 5},
            ]
        }

    # The prompt names scenes by ORDER index, not id — spy on order instead.
    seen_orders: list[int] = []

    async def spy(messages, temperature=0.5):
        import re

        m = re.search(r"^Scene (\d+):", messages[1]["content"], re.MULTILINE)
        if m:
            seen_orders.append(int(m.group(1)))
        return await fake_llm(messages, temperature)

    monkeypatch.setattr("calliope.agent.coverage_agent.generate_structured", spy)

    r = client.post(f"/api/projects/{pid}/expand-clips", json={"scene_ids": [s2["id"]]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert [s["scene_id"] for s in body["scenes"]] == [s2["id"]]
    assert body["total_clips"] == 2
    # Exactly ONE scene went through the LLM coverage pass: scene #2.
    assert seen_orders == [2], f"LLM was invoked for scenes {seen_orders}, expected [2]"

    conn = get_db(settings.db_path)
    try:
        n1 = conn.execute(
            "SELECT COUNT(*) AS n FROM clips WHERE scene_id = ?", (s1["id"],)
        ).fetchone()["n"]
        n2 = conn.execute(
            "SELECT COUNT(*) AS n FROM clips WHERE scene_id = ?", (s2["id"],)
        ).fetchone()["n"]
    finally:
        conn.close()
    assert n1 == 1, f"scene #1 was touched: {n1} clips (default clip only allowed)"
    assert n2 == 2, f"scene #2 should have exactly 2 clips, got {n2}"


# ── clip prompt hash: scene + clip fields ───────────────────────────────


def test_clip_prompt_hash_distinguishes_clips():
    base = {"heading": "INT. BAR", "action": "x", "dialog": "A\nB"}
    c1 = {
        **base,
        "description": "wide",
        "dialog_lines_covered": "[1]",
        "duration_sec": 5,
        "order_index": 1,
    }
    c2 = {
        **base,
        "description": "close",
        "dialog_lines_covered": "[2]",
        "duration_sec": 5,
        "order_index": 2,
    }
    assert _clip_prompt_hash(c1) != _clip_prompt_hash(c2)
    assert _clip_prompt_hash(c1) == _clip_prompt_hash(dict(c1))


def test_fetch_clips_orders_by_scene_then_clip(client):
    pid = _mk_project(client)
    s1 = _add_scene(client, pid, 1)
    s2 = _add_scene(client, pid, 2)
    conn = get_db(settings.db_path)
    try:
        for sid in (s1["id"], s2["id"]):
            conn.execute(
                "INSERT INTO clips (scene_id, project_id, order_index) VALUES (?, ?, 2)",
                (sid, pid),
            )
        conn.commit()
    finally:
        conn.close()
    conn = get_db(settings.db_path)
    try:
        clips = _fetch_clips(conn, pid)
    finally:
        conn.close()
    order = [(c["scene_id"], c["order_index"]) for c in clips]
    assert order == sorted(order)
    assert len(clips) == 4


# ── preview endpoint: clip_id ───────────────────────────────────────────


def test_preview_prompt_accepts_clip_id(client, video_workflow):
    pid = _mk_project(client)
    scene = _add_scene(client, pid, 1, dialog="ANNA\nLine one.")
    clip_id = scene["clips"][0]["id"]
    # Give the default clip a description + dialog coverage so the narrowed
    # beat carries the line into the prompt.
    client.patch(
        f"/api/projects/{pid}/clips/{clip_id}",
        json={"description": "medium on ANNA", "dialog_lines_covered": [1, 2]},
    )
    r = client.post(
        f"/api/jobs/projects/{pid}/preview-prompt",
        json={"clip_id": clip_id},
    )
    assert r.status_code == 200
    body = r.json()
    assert "Line one." in body["prompt"]


def test_generate_videos_accepts_clip_ids(client, video_workflow):
    pid = _mk_project(client)
    scene = _add_scene(client, pid, 1)
    clip_id = scene["clips"][0]["id"]
    r = client.post(
        f"/api/jobs/projects/{pid}/generate-videos",
        json={"clip_ids": [clip_id]},
    )
    assert r.status_code == 200
    jobs = r.json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["clip_id"] == clip_id


# ── agent tool scope: break_into_shots guard + clip addressing ─────────


def _mk_tool_ctx(client, session_id: int, project_id: int):
    from calliope.agent.harness.registry import ToolContext

    return ToolContext(session_id=session_id, project_id=project_id)


def test_break_into_shots_requires_scope(client):
    from calliope.agent.harness.plugins.script import t_break_into_shots

    pid = _mk_project(client)
    _add_scene(client, pid, 1)
    ctx = _mk_tool_ctx(client, session_id=1, project_id=pid)
    result = asyncio.run(t_break_into_shots(ctx, {}))
    assert result["ok"] is False
    assert "scene_ids" in result["error"]


def test_list_clips_labels(client):
    from calliope.agent.harness.plugins.script import t_list_clips

    pid = _mk_project(client)
    scene = _add_scene(client, pid, 3)
    conn = get_db(settings.db_path)
    try:
        conn.execute(
            "INSERT INTO clips (scene_id, project_id, order_index, description) VALUES (?, ?, 2, 'two')",
            (scene["id"], pid),
        )
        conn.commit()
    finally:
        conn.close()
    ctx = _mk_tool_ctx(client, session_id=1, project_id=pid)
    result = asyncio.run(t_list_clips(ctx, {}))
    labels = [c["label"] for c in result["clips"]]
    assert "#3.1" in labels and "#3.2" in labels


def test_delete_clip_tool_protects_last(client):
    from calliope.agent.harness.plugins.script import t_delete_clip

    pid = _mk_project(client)
    scene = _add_scene(client, pid, 1)
    ctx = _mk_tool_ctx(client, session_id=1, project_id=pid)
    result = asyncio.run(t_delete_clip(ctx, {"clip_id": scene["clips"][0]["id"]}))
    assert result["ok"] is False
    assert "at least one clip" in result["error"]


def test_update_clip_by_ref(client):
    from calliope.agent.harness.plugins.script import t_update_clip

    pid = _mk_project(client)
    scene = _add_scene(client, pid, 2)
    ctx = _mk_tool_ctx(client, session_id=1, project_id=pid)
    result = asyncio.run(t_update_clip(ctx, {"ref": "#2.1", "description": "updated", "duration_sec": 7}))
    assert result.get("ok", True) is not False
    assert result["clip"]["description"] == "updated"


def test_enqueue_video_jobs_tool_by_ref(client, video_workflow):
    from calliope.agent.harness.plugins.render import t_enqueue_video_jobs

    pid = _mk_project(client)
    scene = _add_scene(client, pid, 1)
    ctx = _mk_tool_ctx(client, session_id=1, project_id=pid)
    result = asyncio.run(t_enqueue_video_jobs(ctx, {"refs": ["#1.1"]}))
    assert "jobs" in result
    assert result["count"] == 1


def test_enqueue_video_jobs_tool_bulk_guard(client, video_workflow):
    from calliope.agent.harness.plugins.render import t_enqueue_video_jobs

    pid = _mk_project(client)
    scene = _add_scene(client, pid, 1)
    scene_id = scene["id"]
    conn = get_db(settings.db_path)
    try:
        for i in range(2, 6):
            conn.execute(
                "INSERT INTO clips (scene_id, project_id, order_index, description) VALUES (?, ?, ?, 'c')",
                (scene_id, pid, i),
            )
        conn.commit()
    finally:
        conn.close()
    ctx = _mk_tool_ctx(client, session_id=1, project_id=pid)
    result = asyncio.run(t_enqueue_video_jobs(ctx, {"scene_ids": [scene_id]}))
    # scene_ids expands to 6 clips (>3) without explicit user prose — blocked.
    assert result["ok"] is False
    assert "bulk" in result["error"]
