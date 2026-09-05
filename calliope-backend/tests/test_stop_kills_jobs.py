"""Stop must stop the GPU work, not just the LLM turn.

Agent-enqueued jobs stamp session_id into their payload; cancel_by_session
flips every pending/running one to 'cancelled'; the worker's poll loop sees
the flipped row and interrupts the running ComfyUI prompt.
"""
from __future__ import annotations

import asyncio

import pytest

from calliope.queue.manager import queue_manager


@pytest.fixture(autouse=True)
def _scratch_db(monkeypatch, tmp_path):
    import calliope.config as config_module

    monkeypatch.setattr(config_module.settings, "data_dir", tmp_path)
    from calliope.db import get_db, migrate_db

    asyncio.run(migrate_db(tmp_path / "calliope.db"))
    conn = get_db(tmp_path / "calliope.db")
    conn.execute("INSERT INTO projects (id, title) VALUES (1, 'Stop Film')")
    conn.commit()
    conn.close()
    yield


def test_cancel_by_session_flips_only_that_sessions_jobs():
    j1 = queue_manager.enqueue(project_id=1, kind="image", payload={"session_id": 7})
    j2 = queue_manager.enqueue(project_id=1, kind="image", payload={"session_id": 8})
    j3 = queue_manager.enqueue(project_id=1, kind="image", payload={})

    cancelled = queue_manager.cancel_by_session(7)
    assert cancelled == [j1["id"]]

    s1 = queue_manager.get_job(j1["id"])["status"]
    s2 = queue_manager.get_job(j2["id"])["status"]
    s3 = queue_manager.get_job(j3["id"])["status"]
    assert s1 == "failed"  # cancelled carries the failed terminal state
    assert queue_manager.get_job(j1["id"])["error"] == "cancelled"
    assert s2 == "pending"
    assert s3 == "pending"


def test_is_cancelled_reflects_terminal_state():
    j = queue_manager.enqueue(project_id=1, kind="image", payload={"session_id": 9})
    assert queue_manager.is_cancelled(j["id"]) is False
    queue_manager.cancel_by_session(9)
    assert queue_manager.is_cancelled(j["id"]) is True


def test_cancel_endpoint_returns_cancelled_jobs(client):
    from calliope.agent.harness.registry import _db

    conn = _db()
    try:
        conn.execute("INSERT INTO projects (id, title) VALUES (1, 'Stop Film')")
        conn.commit()
    finally:
        conn.close()
    j = queue_manager.enqueue(project_id=1, kind="image", payload={"session_id": 42})
    resp = client.post("/api/agent/sessions/42/cancel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cancelled_jobs"] == [j["id"]]
    assert queue_manager.get_job(j["id"])["status"] == "failed"
