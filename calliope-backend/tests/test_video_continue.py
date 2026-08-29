"""Continue-from-previous scenes: enqueue-time video validation + source resolution."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from calliope.agent.video_agent import enqueue_video_jobs
from calliope.comfyui.client import ComfyUIClient
from calliope.config import settings
from calliope.db import get_db
from calliope.queue.manager import queue_manager
from calliope.queue.worker import queue_worker

# One prompt node, one video input slot — the minimum viable "continue" workflow.
VIDEO_WORKFLOW = {
    "10": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": ""},
        "_meta": {"title": "Main Prompt (Input:prompt)"},
    },
    "20": {
        "class_type": "LoadVideo",
        "inputs": {"file": ""},
        "_meta": {"title": "Previous Clip (Input:video)"},
    },
}

# Same but the video slot is missing entirely — must be rejected at enqueue.
NO_VIDEO_WORKFLOW = {
    "10": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": ""},
        "_meta": {"title": "Main Prompt (Input:prompt)"},
    },
}


def _mk_project(client, title: str) -> int:
    return client.post("/api/projects", json={"title": title}).json()["id"]


def _add_scene(client, pid: int, order: int, **extra) -> dict:
    payload = {"order_index": order, "heading": f"S{order}", **extra}
    return client.post(f"/api/projects/{pid}/scenes", json=payload).json()


def _set_chain(client, pid: int, scene_id: int, on: bool) -> None:
    r = client.patch(
        f"/api/projects/{pid}/scenes/{scene_id}", json={"chain_from_prev": on}
    )
    assert r.status_code == 200


def _insert_workflow(conn, name: str, wf_json: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO workflows (name, kind, workflow_json, input_schema, output_schema, is_enabled)
        VALUES (?, 'video', ?, '[]', '[]', 1)
        """,
        (name, json.dumps(wf_json)),
    )
    conn.commit()
    return int(cur.lastrowid)


def _job_payloads(pid: int) -> list[dict]:
    conn = get_db(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM jobs WHERE project_id = ? AND kind = 'video' "
            "ORDER BY id",
            (pid,),
        ).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]
    finally:
        conn.close()


def _touch_clip(name: str) -> str:
    root = Path(settings.assets_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_bytes(b"mp4-placeholder")
    return str(path)


def test_continue_scene_requires_video_input_role(client):
    pid = _mk_project(client, "No Video WF")
    _add_scene(client, pid, 1)
    scene2 = _add_scene(client, pid, 2)
    _set_chain(client, pid, scene2["id"], True)
    conn = get_db(settings.db_path)
    try:
        _insert_workflow(conn, "no-video", NO_VIDEO_WORKFLOW)
    finally:
        conn.close()

    try:
        asyncio.run(enqueue_video_jobs(pid))
    except ValueError as exc:
        assert "no video input" in str(exc)
        assert "(Input:video)" in str(exc)
    else:
        raise AssertionError("expected ValueError for workflow without video role")

    # Supersede + video_path clear happened for scene 1 before the failure on
    # scene 2; the failing scene must not have produced a job.
    assert len(_job_payloads(pid)) == 1


def test_continue_scene_uses_previous_clip_path(client):
    pid = _mk_project(client, "Prev Clip")
    _add_scene(client, pid, 1)
    scene2 = _add_scene(client, pid, 2)
    _set_chain(client, pid, scene2["id"], True)
    clip = _touch_clip("prev-clip.mp4")
    conn = get_db(settings.db_path)
    try:
        wid = _insert_workflow(conn, "video-wf", VIDEO_WORKFLOW)
        conn.execute(
            "UPDATE scenes SET video_path = ? WHERE order_index = 1 AND project_id = ?",
            (clip, pid),
        )
        conn.commit()
    finally:
        conn.close()

    # Re-generate only the continue scene: scene 1 is not in the batch, so its
    # clip stays on disk and is injected directly (no worker deferral needed).
    jobs = asyncio.run(enqueue_video_jobs(pid, scene_ids=[scene2["id"]]))
    payloads = _job_payloads(pid)
    assert len(payloads) == 1

    video_values = payloads[0]["input_values"]
    # Node 20 (Input:video) holds the previous scene's local clip path.
    assert video_values["20"] == clip
    assert "continue_source" not in payloads[0]
    assert jobs[0]["workflow_id"] == wid


def test_continue_scene_defers_to_worker_without_clip(client):
    pid = _mk_project(client, "Deferred Clip")
    _add_scene(client, pid, 1)
    scene2 = _add_scene(client, pid, 2)
    _set_chain(client, pid, scene2["id"], True)
    conn = get_db(settings.db_path)
    try:
        _insert_workflow(conn, "video-wf", VIDEO_WORKFLOW)
    finally:
        conn.close()

    jobs = asyncio.run(enqueue_video_jobs(pid))
    payloads = _job_payloads(pid)
    assert len(payloads) == 2
    assert payloads[1]["continue_source"] == {"scene_order_index": 2}
    assert "20" not in payloads[1]["input_values"]
    assert jobs[1]["scene_id"] == scene2["id"]


def test_continue_scene_on_first_scene_raises(client):
    pid = _mk_project(client, "First Scene")
    scene1 = _add_scene(client, pid, 1)
    _set_chain(client, pid, scene1["id"], True)
    conn = get_db(settings.db_path)
    try:
        _insert_workflow(conn, "video-wf", VIDEO_WORKFLOW)
    finally:
        conn.close()

    try:
        asyncio.run(enqueue_video_jobs(pid))
    except ValueError as exc:
        assert "no previous clip exists yet" in str(exc)
    else:
        raise AssertionError("expected ValueError for continue on first scene")

    # Nothing was enqueued for the failing scene.
    assert _job_payloads(pid) == []


def test_continue_scene_explicit_value_wins_over_previous(client):
    pid = _mk_project(client, "Explicit Clip")
    _add_scene(client, pid, 1)
    scene2 = _add_scene(client, pid, 2)
    _set_chain(client, pid, scene2["id"], True)
    conn = get_db(settings.db_path)
    try:
        wid = _insert_workflow(conn, "video-wf", VIDEO_WORKFLOW)
    finally:
        conn.close()
    explicit_clip = _touch_clip("user-upload.mp4")

    jobs = asyncio.run(enqueue_video_jobs(pid, input_values_override={"20": explicit_clip}))
    payloads = _job_payloads(pid)
    assert len(payloads) == 2
    assert payloads[1]["input_values"]["20"] == explicit_clip
    assert "continue_source" not in payloads[1]
    assert jobs[1]["workflow_id"] == wid


def test_non_continue_scene_untouched(client):
    pid = _mk_project(client, "Plain Scenes")
    _add_scene(client, pid, 1)
    _add_scene(client, pid, 2)
    conn = get_db(settings.db_path)
    try:
        _insert_workflow(conn, "video-wf", VIDEO_WORKFLOW)
    finally:
        conn.close()

    asyncio.run(enqueue_video_jobs(pid))
    payloads = _job_payloads(pid)
    assert len(payloads) == 2
    for payload in payloads:
        assert "continue_source" not in payload
        assert "chain_from_prev" not in payload
        assert "scene_order_index" not in payload
        assert "chain_replace_value" not in payload
        assert "20" not in payload["input_values"]


def _last_job_row(pid: int) -> dict:
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE project_id = ? AND kind = 'video' ORDER BY id DESC LIMIT 1",
            (pid,),
        ).fetchone()
        assert row is not None
        return dict(row)
    finally:
        conn.close()


def _scene_ids(pid: int) -> list[int]:
    conn = get_db(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM scenes WHERE project_id = ? ORDER BY order_index", (pid,)
        ).fetchall()
        return [r["id"] for r in rows]
    finally:
        conn.close()


async def _fake_history(_client, _prompt_id):
    """A successful ComfyUI history with one video output named out.mp4."""
    return {
        "status": {"status_str": "success", "completed": True},
        "outputs": {
            "99": {"videos": [{"filename": "out.mp4", "subfolder": "", "type": "output"}]}
        },
    }


def _stub_comfy(monkeypatch, captured: dict) -> list[str]:
    """Run the worker's real (non-dry) path with ComfyUI faked in-process.

    The live worker (started by the TestClient lifespan) is paused so it can
    not claim the pending jobs; queue_prompt captures the patched workflow so
    tests can assert on it; upload_video records which local path
    prepare_media_inputs handed it; download_image writes out.mp4. Returns
    the list that collects uploaded local paths.
    """
    queue_manager.paused = True
    uploads: list[str] = []

    async def fake_upload_video(self, path, subfolder="calliope"):
        uploads.append(str(path))
        return f"{subfolder}/{Path(path).name}"

    async def fake_queue_prompt(self, workflow):
        captured.update(workflow)
        return "test-prompt-id"

    async def fake_download(self, filename, subfolder="", folder_type="output", dest=None):
        dest.write_bytes(b"mp4-placeholder")
        return dest

    async def fake_health(self):
        return True

    monkeypatch.setattr(ComfyUIClient, "health", fake_health)
    monkeypatch.setattr(ComfyUIClient, "upload_video", fake_upload_video)
    monkeypatch.setattr(ComfyUIClient, "queue_prompt", fake_queue_prompt)
    monkeypatch.setattr(ComfyUIClient, "download_image", fake_download)
    monkeypatch.setattr(queue_worker, "_poll_history", _fake_history)
    # conftest sets dry_run=True for placeholder generation; the assertions
    # here need the real patch path, so turn it off for the direct _run_job.
    monkeypatch.setattr(settings, "dry_run", False)
    return uploads


def test_worker_resolves_continue_source_into_video_node(client, monkeypatch):
    """Continue_source job: worker injects the earlier clip before patching."""
    pid = _mk_project(client, "Worker Resolve")
    _add_scene(client, pid, 1)
    scene2 = _add_scene(client, pid, 2)
    _set_chain(client, pid, scene2["id"], True)
    clip = _touch_clip("worker-resolve.mp4")
    conn = get_db(settings.db_path)
    try:
        wid = _insert_workflow(conn, "video-wf", VIDEO_WORKFLOW)
        conn.execute(
            "UPDATE scenes SET video_path = ? WHERE order_index = 1 AND project_id = ?",
            (clip, pid),
        )
        conn.commit()
    finally:
        conn.close()

    # Same enqueue shape as the deferred batch case: scene 1 renders first, so
    # at enqueue time scene 2's clip does not exist yet and lands in
    # continue_source. The worker must resolve it before patching.
    jobs = asyncio.run(enqueue_video_jobs(pid))
    assert len(jobs) == 2
    payloads = _job_payloads(pid)
    assert payloads[1].get("continue_source") == {"scene_order_index": 2}

    job = _last_job_row(pid)
    assert job["workflow_id"] == wid
    captured: dict = {}
    uploads = _stub_comfy(monkeypatch, captured)

    # Re-create the previous clip now: enqueue_video_jobs cleared scene 1's
    # video_path row (supersede pass) and the paused real worker can't have
    # rendered it. The row points at the file the earlier job would produce.
    conn = get_db(settings.db_path)
    try:
        conn.execute(
            "UPDATE scenes SET video_path = ? WHERE order_index = 1 AND project_id = ?",
            (clip, pid),
        )
        conn.commit()
    finally:
        conn.close()

    outputs = asyncio.run(queue_worker._run_job(job))
    assert len(outputs) == 1
    # The resolved local path reached prepare_media_inputs, which uploaded it.
    assert uploads == [clip]
    # ...and the video node now carries the uploaded (Comfy-side) file name.
    assert captured["20"]["inputs"]["file"] == f"calliope/{Path(clip).name}"
    # The scene's own clip was refreshed by the job output.
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT video_path FROM scenes WHERE id = ?", (scene2["id"],)
        ).fetchone()
        assert row["video_path"] == outputs[0]
    finally:
        conn.close()


def test_worker_fails_continue_job_without_previous_clip(client, monkeypatch):
    pid = _mk_project(client, "Worker Missing")
    _add_scene(client, pid, 1)
    scene2 = _add_scene(client, pid, 2)
    _set_chain(client, pid, scene2["id"], True)
    conn = get_db(settings.db_path)
    try:
        _insert_workflow(conn, "video-wf", VIDEO_WORKFLOW)
    finally:
        conn.close()

    asyncio.run(enqueue_video_jobs(pid))
    payloads = _job_payloads(pid)
    assert payloads[1].get("continue_source") == {"scene_order_index": 2}
    job = _last_job_row(pid)

    # Run only the continue job: its earlier scene has no clip on disk yet,
    # so the worker must fail it with a clear message (the loop records it
    # via mark_failed + job.failed event).
    captured: dict = {}
    _stub_comfy(monkeypatch, captured)
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(queue_worker._run_job(job))
    message = str(excinfo.value)
    assert "Continue scene 2" in message
    assert "previous clip (scene 1) has no video file" in message
    assert "generate the earlier clip first" in message
    # Nothing reached ComfyUI.
    assert captured == {}


def test_worker_passes_plain_job_unchanged(client, monkeypatch):
    pid = _mk_project(client, "Worker Plain")
    _add_scene(client, pid, 1)
    conn = get_db(settings.db_path)
    try:
        wid = _insert_workflow(conn, "video-wf", VIDEO_WORKFLOW)
    finally:
        conn.close()

    jobs = asyncio.run(enqueue_video_jobs(pid, scene_ids=[_scene_ids(pid)[0]]))
    payloads = _job_payloads(pid)
    assert len(payloads) == 1
    assert "continue_source" not in payloads[0]
    job = _last_job_row(pid)

    captured: dict = {}
    _stub_comfy(monkeypatch, captured)
    outputs = asyncio.run(queue_worker._run_job(job))

    assert len(outputs) == 1
    # No video value was injected; the LoadVideo node keeps its empty default.
    assert captured["20"]["inputs"]["file"] == ""
    assert captured["10"]["inputs"]["text"]  # scene prompt was filled
    assert job["workflow_id"] == wid
