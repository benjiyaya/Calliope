"""Scene video_settings persistence (issue #28): PATCH round-trip, enqueue merge."""
from __future__ import annotations

import json

from calliope.agent.video_agent import enqueue_video_jobs
from calliope.config import settings
from calliope.db import get_db


def _mk_project(client, title: str) -> int:
    return client.post("/api/projects", json={"title": title}).json()["id"]


def _add_scene(client, pid: int, order: int, **extra) -> dict:
    payload = {"order_index": order, "heading": f"S{order}", **extra}
    return client.post(f"/api/projects/{pid}/scenes", json=payload).json()


def test_scene_patch_video_settings_roundtrip(client):
    pid = _mk_project(client, "Settings RT")
    scene = _add_scene(client, pid, 1)

    settings_payload = {
        "input_values": {"102": 1280, "209": "my prompt"},
        "clip_source": "auto",
        "form_workflow_id": 3,
    }
    r = client.patch(
        f"/api/projects/{pid}/scenes/{scene['id']}",
        json={"video_settings": settings_payload},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["video_settings"] == settings_payload

    # GET path parses the column back out too
    r = client.get(f"/api/projects/{pid}/scenes")
    scenes = r.json()["scenes"]
    assert scenes[0]["video_settings"] == settings_payload


def test_scene_patch_video_settings_alongside_other_fields(client):
    pid = _mk_project(client, "Settings mixed")
    scene = _add_scene(client, pid, 1)

    r = client.patch(
        f"/api/projects/{pid}/scenes/{scene['id']}",
        json={"heading": "Renamed", "video_settings": {"input_values": {"5": "x.wav"}}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["heading"] == "Renamed"
    assert body["video_settings"]["input_values"]["5"] == "x.wav"


def test_video_settings_survive_migration_column(client):
    """Existing DBs get the column via migrate_db — writes/reads work on it."""
    conn = get_db(settings.db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(scenes)").fetchall()}
        assert "video_settings_json" in cols
    finally:
        conn.close()


def _insert_h3_workflow(conn, name: str) -> int:
    wf = {
        "10": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": ""},
            "_meta": {"title": "Main Prompt (Input:prompt)"},
        },
        "20": {
            "class_type": "PrimitiveInt",
            "inputs": {"value": 6},
            "_meta": {"title": "Duration (Input:duration)"},
        },
    }
    cur = conn.execute(
        """
        INSERT INTO workflows (name, kind, workflow_json, input_schema, output_schema,
                               prompt_profile, is_enabled)
        VALUES (?, 'video', ?, '[]', '[]', 'minimax_h3_ref', 1)
        """,
        (name, json.dumps(wf)),
    )
    conn.commit()
    return cur.lastrowid


def test_enqueue_merges_stored_input_values(client, monkeypatch):
    """Batch enqueue with no form values must honor the saved per-scene setup."""
    pid = _mk_project(client, "Merge stored")
    scene = _add_scene(client, pid, 1)

    conn = get_db(settings.db_path)
    try:
        wf_id = _insert_h3_workflow(conn, "H3 merge")
        stored = {"input_values": {"20": 10}}
        conn.execute(
            "UPDATE scenes SET workflow_id = ?, video_settings_json = ? WHERE id = ?",
            (wf_id, json.dumps(stored), scene["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    # Skip the LLM rewrite — the fallback template is deterministic
    async def fake_rewrite(scene_, subjects):
        return "fallback"

    monkeypatch.setattr("calliope.agent.video_agent._h3_rewrite", fake_rewrite)

    from calliope.queue.manager import queue_manager

    queue_manager.paused = True
    try:
        jobs = asyncio_run(
            enqueue_video_jobs(pid, scene_ids=[scene["id"]])
        )
        assert len(jobs) == 1
        raw = json.loads(jobs[0]["payload_json"])
        values = raw["input_values"]
        assert values["20"] == 10  # stored setup applied
        assert raw["prompt"] == "fallback"
    finally:
        queue_manager.paused = False


def test_preview_prompt_endpoint_h3_profile(client, monkeypatch):
    """Preview resolves the H3 rewrite without enqueueing anything."""
    from calliope.queue.manager import queue_manager

    pid = _mk_project(client, "Preview H3")
    scene = _add_scene(client, pid, 1)

    conn = get_db(settings.db_path)
    try:
        wf_id = _insert_h3_workflow(conn, "H3 preview")
        conn.execute(
            "UPDATE scenes SET workflow_id = ? WHERE id = ?", (wf_id, scene["id"])
        )
        conn.commit()
    finally:
        conn.close()

    async def fake_rewrite(scene_, subjects, **kwargs):
        return "H3 REWRITE"

    monkeypatch.setattr("calliope.agent.video_agent._h3_rewrite", fake_rewrite)

    queue_manager.paused = True
    try:
        r = client.post(
            f"/api/jobs/projects/{pid}/preview-prompt",
            json={"scene_id": scene["id"]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["prompt"] == "H3 REWRITE"
        assert body["profile"] == "minimax_h3_ref"
        assert body["from_draft"] is False
        assert body["based_on"]
        # No jobs were created by a preview
        jobs = client.get(f"/api/jobs?project_id={pid}").json()
        video_jobs = [j for j in jobs if j["kind"] == "video"]
        assert video_jobs == []
    finally:
        queue_manager.paused = False


def test_preview_prompt_fresh_draft_shortcircuits_llm(client, monkeypatch):
    """A saved fresh draft is returned as-is — no LLM call."""
    from calliope.agent.video_agent import preview_scene_prompt

    pid = _mk_project(client, "Preview draft")
    scene = _add_scene(client, pid, 1)

    conn = get_db(settings.db_path)
    try:
        wf_id = _insert_h3_workflow(conn, "H3 draft")
        conn.execute(
            "UPDATE scenes SET workflow_id = ? WHERE id = ?", (wf_id, scene["id"])
        )
        conn.commit()
    finally:
        conn.close()

    # Compute the hash the backend would, then store the draft with it
    from calliope.agent.video_agent import _scene_prompt_hash

    conn = get_db(settings.db_path)
    try:
        fresh_row = conn.execute(
            "SELECT * FROM scenes WHERE id = ?", (scene["id"],)
        ).fetchone()
        from calliope.db import row_to_dict

        fresh_scene = row_to_dict(fresh_row)
        # character_ids aren't on the row — hash treats them via scene dict
        fresh_scene["character_ids"] = []
        stored = {
            "prompt_draft": "MY SAVED DRAFT",
            "prompt_draft_meta": {"based_on": _scene_prompt_hash(fresh_scene)},
        }
        conn.execute(
            "UPDATE scenes SET video_settings_json = ? WHERE id = ?",
            (json.dumps(stored), scene["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    called = []

    async def fake_rewrite(scene_, subjects, **kwargs):
        called.append(1)
        return "SHOULD NOT BE USED"

    monkeypatch.setattr("calliope.agent.video_agent._h3_rewrite", fake_rewrite)

    result = asyncio_run(preview_scene_prompt(pid, scene["id"]))
    assert result["prompt"] == "MY SAVED DRAFT"
    assert result["from_draft"] is True
    assert called == []  # LLM never invoked


def test_preview_prompt_dead_llm_returns_deterministic_fallback(client, monkeypatch):
    """A failing LLM rewrite falls back to minimax_h3_ref_fallback — never 500s.

    This is the failure mode behind the Discord report: a dead LLM endpoint
    must not leave the preview modal hanging or erroring out.
    """
    from calliope.agent import video_agent

    pid = _mk_project(client, "Preview dead LLM")
    scene = _add_scene(client, pid, 1, action="A lone rider crosses the salt flats.")

    conn = get_db(settings.db_path)
    try:
        wf_id = _insert_h3_workflow(conn, "H3 dead")
        conn.execute(
            "UPDATE scenes SET workflow_id = ? WHERE id = ?", (wf_id, scene["id"])
        )
        conn.commit()
    finally:
        conn.close()

    class DeadClient:
        def __init__(self, *args, **kwargs):
            pass

        async def chat(self, messages, temperature=0.7, response_format=None):
            raise RuntimeError("connection refused")

        async def close(self):
            return None

    monkeypatch.setattr(
        video_agent,
        "LLMClient",
        type("LLMClientStub", (), {"for_role": staticmethod(lambda role, **kw: DeadClient())}),
    )

    result = asyncio_run(video_agent.preview_scene_prompt(pid, scene["id"]))
    assert result["profile"] == "minimax_h3_ref"
    assert result["from_draft"] is False
    # Deterministic template content, not an exception and not empty
    assert "salt flats" in result["prompt"]
    assert result["prompt"].startswith("subject_definitions:")
    # No jobs were enqueued by the preview
    jobs = client.get(f"/api/jobs?project_id={pid}").json()
    assert [j for j in jobs if j["kind"] == "video"] == []


def test_enqueue_prompts_override(client, monkeypatch):
    """Confirmed prompt from the review modal wins over the rewrite."""
    pid = _mk_project(client, "Prompt override")
    scene = _add_scene(client, pid, 1)

    conn = get_db(settings.db_path)
    try:
        wf_id = _insert_h3_workflow(conn, "H3 override")
        conn.execute(
            "UPDATE scenes SET workflow_id = ? WHERE id = ?", (wf_id, scene["id"])
        )
        conn.commit()
    finally:
        conn.close()

    async def fake_rewrite(scene_, subjects):
        return "LLM VERSION"

    monkeypatch.setattr("calliope.agent.video_agent._h3_rewrite", fake_rewrite)

    from calliope.queue.manager import queue_manager

    queue_manager.paused = True
    try:
        jobs = asyncio_run(
            enqueue_video_jobs(pid, scene_ids=[scene["id"]], prompts={scene["id"]: "CONFIRMED"})
        )
        raw = json.loads(jobs[0]["payload_json"])
        assert raw["prompt"] == "CONFIRMED"
    finally:
        queue_manager.paused = False


def test_preview_prompt_prose_profile(client):
    """Prose workflows return the deterministic scene_video_prompt."""
    pid = _mk_project(client, "Preview prose")
    scene = _add_scene(client, pid, 1, action="A knight rides at dawn.")

    conn = get_db(settings.db_path)
    try:
        wf = {
            "10": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": ""},
                "_meta": {"title": "Main Prompt (Input:prompt)"},
            }
        }
        cur = conn.execute(
            """
            INSERT INTO workflows (name, kind, workflow_json, input_schema, output_schema, is_enabled)
            VALUES (?, 'video', ?, '[]', '[]', 1)
            """,
            ("Prose WF", json.dumps(wf)),
        )
        conn.execute(
            "UPDATE scenes SET workflow_id = ? WHERE id = ?", (cur.lastrowid, scene["id"])
        )
        conn.commit()
    finally:
        conn.close()

    r = client.post(
        f"/api/jobs/projects/{pid}/preview-prompt",
        json={"scene_id": scene["id"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["profile"] == "prose"
    assert "knight" in body["prompt"]


def test_preview_prompt_missing_scene_400(client):
    pid = _mk_project(client, "Preview missing")
    r = client.post(
        f"/api/jobs/projects/{pid}/preview-prompt",
        json={"scene_id": 99999},
    )
    assert r.status_code == 400


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
