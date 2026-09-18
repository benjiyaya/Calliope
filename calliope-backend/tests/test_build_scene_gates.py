"""Build Scene Brief/Cut gates (shot-composer-blockout harness)."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from calliope.config import settings
from calliope.db import get_db, migrate_db
from calliope.agent.harness.registry import ToolContext


@pytest.fixture(autouse=True, scope="module")
def _never_touch_real_db():
    prev = settings.data_dir
    with tempfile.TemporaryDirectory() as tmp:
        settings.data_dir = Path(tmp)
        settings.assets_dir = Path(tmp) / "assets"
        asyncio.run(migrate_db(settings.db_path))
        yield
    settings.data_dir = prev


def _mk_session() -> int:
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO agent_sessions (title, project_id) VALUES (?, NULL)",
            ("gate-test",),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _run(sid: int, tool: str, args: dict | None = None):
    from calliope.agent.harness.tools import TOOLS

    t = TOOLS[tool]
    return asyncio.run(
        t.executor(ToolContext(session_id=sid, project_id=None), args or {})
    )


def test_gate_tools_visible_in_sandbox():
    from calliope.agent.harness.tools import openai_tools_payload

    ctx = ToolContext(session_id=_mk_session(), project_id=None)
    names = {e["function"]["name"] for e in openai_tools_payload(ctx)}
    assert "record_build_scene_gate" in names
    assert "get_build_scene_gates" in names
    assert "get_scene" in names


def test_get_scene_surfaces_gates_empty():
    sid = _mk_session()
    out = _run(sid, "get_scene")
    assert out["ok"] is True
    assert out["gates"] == {"brief": None, "cut": None}


def test_record_brief_and_cut_gates():
    sid = _mk_session()
    brief = _run(
        sid, "record_build_scene_gate", {"gate": "brief", "note": "10s city drone"}
    )
    assert brief["ok"] is True
    assert brief["gate"] == "brief"
    assert brief["gates"]["brief"]["approved_at"]
    assert brief["gates"]["brief"]["source"] == "agent"
    assert brief["gates"]["brief"]["note"] == "10s city drone"
    assert brief["gates"]["cut"] is None

    cut = _run(sid, "record_build_scene_gate", {"gate": "cut"})
    assert cut["ok"] is True
    assert cut["gates"]["cut"]["approved_at"]
    assert cut["gates"]["brief"]["approved_at"]

    gates = _run(sid, "get_build_scene_gates")
    assert gates["gates"]["brief"]["approved_at"]
    assert gates["gates"]["cut"]["approved_at"]

    scene = _run(sid, "get_scene")
    assert scene["gates"]["cut"]["approved_at"]


def test_mutate_soft_warns_without_brief():
    sid = _mk_session()
    out = _run(sid, "add_object", {"type": "cube"})
    assert out["ok"] is True
    assert "warning" in out
    assert "brief" in out["warning"].lower()
    assert "ask_user" in out["warning"]
    assert "Lock the Build Scene brief?" in out["warning"]
    assert "instruction" in out

    _run(sid, "record_build_scene_gate", {"gate": "brief"})
    out2 = _run(sid, "add_object", {"type": "sphere"})
    assert out2["ok"] is True
    assert "warning" not in out2


def test_clear_scene_preserves_gates():
    sid = _mk_session()
    _run(sid, "record_build_scene_gate", {"gate": "brief"})
    _run(sid, "add_object", {"type": "cube"})
    cleared = _run(sid, "clear_scene")
    assert cleared["ok"] is True
    assert cleared["gates"]["brief"]["approved_at"]
    scene = _run(sid, "get_scene")
    assert scene["objects"] == []
    assert scene["gates"]["brief"]["approved_at"]


def test_patch_preserves_gates_when_client_omits(client):
    import json

    sid = _mk_session()
    shot_id = _run(sid, "get_scene")["shot_id"]
    _run(sid, "record_build_scene_gate", {"gate": "cut", "note": "keepme"})
    # Client PATCH without gates (legacy autosave shape)
    scene = {"objects": [{"id": "o1", "type": "cube", "name": "Cube 01"}]}
    resp = client.patch(
        f"/api/shots/{shot_id}",
        json={"scene_json": json.dumps(scene)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    gates = (body.get("scene") or {}).get("gates") or {}
    assert gates.get("cut", {}).get("approved_at")
    assert gates["cut"].get("note") == "keepme"


def test_ui_post_gates_records_cut(client):
    sid = _mk_session()
    shot_id = _run(sid, "get_scene")["shot_id"]
    resp = client.post(
        f"/api/shots/{shot_id}/gates",
        json={"gate": "cut", "source": "ui", "note": "UI approve"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["gate"] == "cut"
    assert body["gates"]["cut"]["approved_at"]
    assert body["gates"]["cut"]["source"] == "ui"
    assert body["gates"]["cut"]["note"] == "UI approve"
