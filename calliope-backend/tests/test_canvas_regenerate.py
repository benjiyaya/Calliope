"""Card regenerate enqueues a new job, and composer effort reaches the turn."""
from __future__ import annotations

import json
import time

import calliope.config as config_module
from calliope.agent.runtime.main_agent import TurnResult
from calliope.db import get_db


def _workflow(conn) -> int:
    wf_json = json.dumps(
        {
            "10": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": ""},
                "_meta": {"title": "Main Prompt (Input:prompt)"},
            }
        }
    )
    cur = conn.execute(
        """
        INSERT INTO workflows (name, kind, workflow_json, input_schema, output_schema, is_enabled)
        VALUES ('card', 'image', ?, '[]', '[]', 1)
        """,
        (wf_json,),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_regenerate_queues_a_new_job_with_the_edited_prompt(client):
    pid = client.post("/api/projects", json={"title": "Regen", "idea": "x"}).json()["id"]
    canvas_id = client.post("/api/canvas", json={"project_id": pid}).json()["canvas"]["id"]
    conn = get_db(config_module.settings.db_path)
    try:
        workflow_id = _workflow(conn)
    finally:
        conn.close()
    node = client.post(
        f"/api/canvas/{canvas_id}/nodes",
        json={
            "type": "image",
            "title": "portrait",
            "workflow_id": workflow_id,
            "input_values_json": json.dumps({"10": "old prompt"}),
        },
    ).json()
    res = client.post(
        f"/api/canvas/{canvas_id}/nodes/{node['id']}/regenerate",
        json={"prompt": "a rider at dawn"},
    )
    assert res.status_code == 200, res.text
    job_id = res.json()["job"]["id"]
    conn = get_db(config_module.settings.db_path)
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        conn.close()
    payload = json.loads(row["payload_json"])
    assert payload["prompt"] == "a rider at dawn"
    assert payload["input_values"]["10"] == "a rider at dawn"
    assert payload["canvas_id"] == canvas_id
    assert payload["source"] == "canvas"
    assert row["project_id"] == pid
    assert row["workflow_id"] == workflow_id


def test_regenerate_without_a_workflow_is_refused(client):
    pid = client.post("/api/projects", json={"title": "Bare", "idea": "x"}).json()["id"]
    canvas_id = client.post("/api/canvas", json={"project_id": pid}).json()["canvas"]["id"]
    node = client.post(
        f"/api/canvas/{canvas_id}/nodes",
        json={"type": "image", "title": "bare"},
    ).json()
    res = client.post(
        f"/api/canvas/{canvas_id}/nodes/{node['id']}/regenerate",
        json={"prompt": "hello"},
    )
    assert res.status_code == 422


def test_sandbox_regenerate_stamps_the_canvas(client):
    sid = client.post("/api/agent/sessions", json={"title": "sand"}).json()["id"]
    canvas_id = client.post("/api/canvas", json={"agent_session_id": sid}).json()["canvas"]["id"]
    conn = get_db(config_module.settings.db_path)
    try:
        workflow_id = _workflow(conn)
    finally:
        conn.close()
    node = client.post(
        f"/api/canvas/{canvas_id}/nodes",
        json={"type": "video", "title": "clip", "workflow_id": workflow_id},
    ).json()
    res = client.post(
        f"/api/canvas/{canvas_id}/nodes/{node['id']}/regenerate",
        json={"prompt": "night street"},
    )
    assert res.status_code == 200, res.text
    conn = get_db(config_module.settings.db_path)
    try:
        row = conn.execute(
            "SELECT project_id, payload_json FROM jobs WHERE id = ?",
            (res.json()["job"]["id"],),
        ).fetchone()
        scratch = conn.execute(
            "SELECT status FROM projects WHERE id = ?", (row["project_id"],)
        ).fetchone()
    finally:
        conn.close()
    assert scratch["status"] == "system"
    assert json.loads(row["payload_json"])["canvas_id"] == canvas_id


def test_composer_effort_is_stored_and_passed(client, monkeypatch):
    seen: dict[str, str | None] = {}

    async def fake_execute(*_args, **kwargs):
        seen["thinking"] = kwargs.get("thinking")
        return TurnResult(text="ok", call=None, task_status="verified")

    monkeypatch.setattr("calliope.agent.harness.runner.execute", fake_execute)
    pid = client.post("/api/projects", json={"title": "Effort", "idea": "x"}).json()["id"]
    sid = client.post(
        "/api/agent/sessions", json={"project_id": pid, "title": "chat"}
    ).json()["id"]
    bad = client.post(
        f"/api/agent/sessions/{sid}/messages",
        json={"content": "How many scenes?", "thinking": "turbo"},
    )
    assert bad.status_code == 422
    res = client.post(
        f"/api/agent/sessions/{sid}/messages",
        json={"content": "How many scenes?", "thinking": "low"},
    )
    assert res.status_code == 200, res.text
    conn = get_db(config_module.settings.db_path)
    try:
        row = conn.execute(
            "SELECT data_json FROM agent_events WHERE session_id = ? AND type = 'user/message'",
            (sid,),
        ).fetchone()
    finally:
        conn.close()
    assert json.loads(row["data_json"])["thinking"] == "low"
    for _ in range(40):
        if "thinking" in seen:
            break
        time.sleep(0.05)
    assert seen.get("thinking") == "low"
