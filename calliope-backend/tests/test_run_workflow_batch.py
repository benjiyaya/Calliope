"""Test for run_workflow batch form: character_ids=[a, b] fans out one job per
character (the "sheets for Kael AND Lila" single-call contract)."""
from __future__ import annotations

import asyncio

import pytest

from calliope.agent.harness.registry import ToolContext, _db


@pytest.fixture(autouse=True)
def _scratch_db(monkeypatch, tmp_path):
    import calliope.config as config_module

    monkeypatch.setattr(config_module.settings, "data_dir", tmp_path)
    from calliope.db import migrate_db

    asyncio.run(migrate_db(tmp_path / "calliope.db"))
    yield


def _make_wf(conn) -> int:
    # Minimal role-tagged API-format workflow: (Input:prompt) present
    wf_json = '{"12": {"class_type": "CLIPTextEncode", "inputs": {}, "_meta": {"title": "P (Input:prompt)"}}}'
    cur = conn.execute(
        """
        INSERT INTO workflows (name, kind, workflow_json, input_schema, output_schema,
                               prompt_profile, is_enabled)
        VALUES ('sheet', 'image', ?, '[]', '[]', 'prose', 1)
        """,
        (wf_json,),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_batch_character_ids_fans_out(client):
    conn = _db()
    try:
        cur = conn.execute("INSERT INTO projects (title) VALUES ('Batch Film')")
        pid = int(cur.lastrowid)
        c1 = conn.execute(
            "INSERT INTO characters (project_id, name) VALUES (?, 'Kael')", (pid,)
        ).lastrowid
        c2 = conn.execute(
            "INSERT INTO characters (project_id, name) VALUES (?, 'Lila')", (pid,)
        ).lastrowid
        conn.commit()
        wid = _make_wf(conn)
    finally:
        conn.close()

    session = client.post("/api/agent/sessions", json={}).json()
    client.patch(
        f"/api/agent/sessions/{session['id']}", json={"project_id": pid}
    ) if False else None

    from calliope.agent.harness.registry import ToolContext
    from calliope.agent.harness import log as session_log
    from calliope.agent.harness.tools import execute_tool

    session_log.append_event(
        session["id"],
        session_log.USER_MESSAGE,
        {"content": "generate character sheets for both"},
    )
    ctx = ToolContext(session_id=session["id"], project_id=pid)

    result = asyncio.run(
        execute_tool(
            ctx,
            "run_workflow",
            {
                "workflow_id": wid,
                "character_ids": [int(c1), int(c2)],
                "prompts_by_character": {
                    str(c1): "Lord Kael, 45, black silk robes",
                    str(c2): "Lila, 19, red auction marks",
                },
            },
        )
    )
    assert result.get("ok") is True, result
    assert result.get("count") == 2, result
    assert result.get("requested") == 2
    assert result.get("failed") == []
    # Each job carries its own character_id so the worker auto-files outputs
    job_pids = {j["id"]: j for j in result["jobs"]}
    assert len(job_pids) == 2
    conn = _db()
    try:
        payload_chars = set()
        for jid in job_pids:
            row = conn.execute(
                "SELECT payload_json FROM jobs WHERE id = ?", (jid,)
            ).fetchone()
            import json as _json

            payload = _json.loads(row["payload_json"])
            payload_chars.add(payload.get("character_id"))
            assert payload.get("prompt"), "each job carries its prompt"
    finally:
        conn.close()
    assert payload_chars == {int(c1), int(c2)}