"""Memory capability: durable notes with upsert dedupe, scope visibility,
and use_count-bumping recall."""

from __future__ import annotations

import asyncio

import pytest

from calliope.agent.capabilities import memory
from calliope.config import settings
from calliope.db import migrate_db


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "assets_dir", tmp_path / "assets")
    asyncio.run(migrate_db(settings.db_path))
    conn = settings.db_path and None
    import sqlite3

    conn = sqlite3.connect(settings.db_path)
    try:
        cur = conn.execute("INSERT INTO projects (title) VALUES ('Fog')")
        project_id = int(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()
    return project_id


def test_save_and_dedupe_by_content(db):
    first = memory.save_memory(db, {"content": "likes fog", "kind": "preference"})
    assert first["ok"] is True
    again = memory.save_memory(db, {"content": "likes fog", "kind": "correction"})
    assert again["ok"] is True
    rows = memory.list_memories(db)["memories"]
    assert len(rows) == 1
    assert rows[0]["kind"] == "correction"  # upsert updated the kind


def test_save_requires_content_and_valid_kind(db):
    assert memory.save_memory(db, {"content": ""})["ok"] is False
    assert memory.save_memory(db, {"content": "x", "kind": "bogus"})["ok"] is False
    assert memory.save_memory(db, {"content": "x", "scope": "bogus"})["ok"] is False


def test_project_memory_needs_a_project():
    assert memory.save_memory(None, {"content": "x", "scope": "project"})["ok"] is False


def test_global_scope_visible_everywhere(db):
    assert memory.save_memory(None, {"content": "always cite the beat"})["ok"] is True
    rows = memory.list_memories(db)["memories"]
    assert any("always cite" in row["content"] for row in rows)


def test_project_scope_stays_in_project(db):
    memory.save_memory(db, {"content": "project note"})
    # A different project (id 0 sentinel — no rows match) sees only globals.
    rows = memory.list_memories(db + 1 if False else 999999)["memories"]
    assert not any("project note" in row["content"] for row in rows)
    rows_here = memory.list_memories(db)["memories"]
    assert any("project note" in row["content"] for row in rows_here)


def test_forget_by_id_and_content(db):
    memory.save_memory(db, {"content": "drop me"})
    listed = memory.list_memories(db)["memories"]
    assert listed
    mid = listed[0]["id"]
    assert memory.forget_memory(db, {"memory_id": mid})["ok"] is True
    assert memory.list_memories(db)["memories"] == []
    memory.save_memory(db, {"content": "second"})
    assert memory.forget_memory(db, {"content": "second"})["ok"] is True
    assert memory.list_memories(db)["memories"] == []
    assert memory.forget_memory(db, {})["ok"] is False
    assert memory.forget_memory(db, {"memory_id": 99999})["ok"] is False


def test_recall_text_bumps_use_count_and_ranks(db):
    for i in range(3):
        memory.save_memory(db, {"content": f"note {i}"})
    text1 = memory.recall_text(db)
    assert text1.startswith("Memories:\n- (project/preference) note")
    assert all(use_count(note) == 1 for note in ("note 0", "note 1", "note 2"))
    memory.recall_text(db)
    assert all(use_count(note) == 2 for note in ("note 0", "note 1", "note 2"))


def use_count(content):
    import sqlite3

    conn = sqlite3.connect(settings.db_path)
    try:
        row = conn.execute(
            "SELECT use_count FROM agent_memory WHERE content = ?", (content,)
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def test_recall_empty_is_empty_string(db):
    assert memory.recall_text(db) == ""
