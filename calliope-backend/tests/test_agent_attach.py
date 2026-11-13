"""attach_image: filing an existing image onto a character/location/item
without queueing anything. Runner cancel keeps settling the session idle."""

from __future__ import annotations

import asyncio

import pytest

from calliope.agent.capabilities import cast
from calliope.agent.harness.runner import runner as global_runner
from calliope.config import settings
from calliope.db import get_db, migrate_db


@pytest.fixture
def linked(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "assets_dir", tmp_path / "assets")
    asyncio.run(migrate_db(settings.db_path))
    (settings.assets_dir).mkdir(parents=True, exist_ok=True)
    conn = get_db(settings.db_path)
    try:
        cur = conn.execute("INSERT INTO projects (title) VALUES ('Fog')")
        project_id = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO characters (project_id, name) VALUES (?, 'Mara')",
            (project_id,),
        )
        character_id = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO locations (project_id, name) VALUES (?, 'Dock')",
            (project_id,),
        )
        location_id = int(cur.lastrowid)
        cur = conn.execute(
            "INSERT INTO agent_sessions (project_id, title, status) VALUES (?, 'chat', 'idle')",
            (project_id,),
        )
        session_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    image = settings.assets_dir / "mara.png"
    image.write_bytes(b"\x89PNG fake")
    return {
        "project_id": project_id,
        "character_id": character_id,
        "location_id": location_id,
        "session_id": session_id,
        "image": str(image),
    }


def _column(db, table, entity_id, column):
    conn = get_db(db)
    try:
        row = conn.execute(
            f"SELECT {column} FROM {table} WHERE id = ?", (entity_id,)
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def test_attach_by_character_id(linked):
    out = cast.attach_image(
        linked["project_id"],
        {"kind": "character", "id": linked["character_id"], "path": linked["image"]},
    )
    assert out["ok"] is True
    assert _column(settings.db_path, "characters", linked["character_id"], "sheet_path") == linked["image"]


def test_attach_portrait_target(linked):
    out = cast.attach_image(
        linked["project_id"],
        {
            "entity": "character",
            "id": linked["character_id"],
            "path": linked["image"],
            "target": "portrait",
        },
    )
    assert out["ok"] is True
    assert _column(settings.db_path, "characters", linked["character_id"], "portrait_path") == linked["image"]


def test_attach_location_writes_reference_path(linked):
    out = cast.attach_image(
        linked["project_id"],
        {"kind": "location", "id": linked["location_id"], "path": linked["image"]},
    )
    assert out["ok"] is True
    assert _column(settings.db_path, "locations", linked["location_id"], "reference_image_path") == linked["image"]


def test_attach_wrong_project_refuses(linked):
    out = cast.attach_image(
        linked["project_id"] + 1,
        {"kind": "character", "id": linked["character_id"], "path": linked["image"]},
    )
    assert out["ok"] is False


def test_path_outside_assets_refused(linked):
    outside = str(linked["image"]).replace("assets", "elsewhere")
    out = cast.attach_image(
        linked["project_id"],
        {"kind": "character", "id": linked["character_id"], "path": outside},
    )
    assert out["ok"] is False


def test_no_attach_args_is_noop(linked):
    for bad in (
        {"kind": "vehicle", "id": 1, "path": linked["image"]},
        {"kind": "character", "path": linked["image"]},
        {"kind": "character", "id": 1},
        {},
    ):
        assert cast.attach_image(linked["project_id"], bad)["ok"] is False


def test_cancel_settles_idle_and_reports_false_when_idle(linked):
    sid = linked["session_id"]
    # Nothing running → cancel is a no-op that reports False.
    assert asyncio.run(global_runner.cancel(sid)) is False
    assert _status(sid) == "idle"
    # A "running" session (no live task) cancels cleanly to idle.
    global_runner._running.add(sid)
    try:
        assert asyncio.run(global_runner.cancel(sid)) is True
    finally:
        global_runner._running.discard(sid)
    assert _status(sid) == "idle"


def _status(session_id):
    conn = get_db(settings.db_path)
    try:
        row = conn.execute(
            "SELECT status FROM agent_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return row[0]
    finally:
        conn.close()
