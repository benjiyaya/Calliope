"""Canvas layout: entity rows must not stack, artifacts fill a grid.

The canvas/65 incident: ENTITY_ROW_HEIGHT=120 against ~190px cards made
entity nodes stack on each other, and artifacts went into one vertical
strip at x=1000 (colliding with item/scene columns, growing 9000px tall).
"""
from __future__ import annotations

import asyncio

import pytest

from calliope.routers.canvas import (
    ARTIFACT_COL_WIDTH,
    ARTIFACT_GRID_COLS,
    ARTIFACT_GRID_X,
    ARTIFACT_ROW_HEIGHT,
    ENTITY_COLUMNS,
    ENTITY_ROW_HEIGHT,
    next_artifact_slot,
    seed_entities,
)
from calliope.db import get_db


@pytest.fixture(autouse=True)
def _scratch_db(monkeypatch, tmp_path):
    import calliope.config as config_module

    monkeypatch.setattr(config_module.settings, "data_dir", tmp_path)
    from calliope.db import migrate_db

    asyncio.run(migrate_db(tmp_path / "calliope.db"))
    yield


def _conn():
    import calliope.config as config_module

    return get_db(config_module.settings.db_path)


def test_entity_row_height_clears_card_height():
    """Entity cards are ~190px tall; rows must be taller than that."""
    assert ENTITY_ROW_HEIGHT >= 200


def test_seed_entities_stack_below_existing():
    conn = _conn()
    try:
        conn.execute("INSERT INTO projects (id, title) VALUES (1, 'Layout')")
        conn.execute("INSERT INTO canvas (id, project_id, title) VALUES (1, 1, 'c')")
        for i, name in enumerate(("A", "B", "C")):
            conn.execute(
                "INSERT INTO characters (id, project_id, name) VALUES (?, 1, ?)",
                (i + 1, name),
            )
        conn.commit()
        seed_entities(conn, 1, 1)
        rows = conn.execute(
            "SELECT y FROM canvas_node WHERE canvas_id = 1 AND type = 'entity' "
            "AND entity_type = 'character' ORDER BY y"
        ).fetchall()
        ys = [r["y"] for r in rows]
        assert len(ys) == 3
        # Each row sits a full ENTITY_ROW_HEIGHT below the previous — no overlap
        for a, b in zip(ys, ys[1:]):
            assert b - a == ENTITY_ROW_HEIGHT
    finally:
        conn.close()


def test_next_artifact_slot_fills_grid_row_major():
    conn = _conn()
    try:
        conn.execute("INSERT INTO canvas (id, title) VALUES (2, 'grid')")
        conn.commit()
        # Each call must see the previously placed node, so insert as we go
        slots = []
        for _ in range(4):
            x, y = next_artifact_slot(conn, 2)
            conn.execute(
                "INSERT INTO canvas_node (canvas_id, type, x, y) VALUES (2, 'image', ?, ?)",
                (x, y),
            )
            conn.commit()
            slots.append((x, y))
        # Row-major: 3 per row, then wrap
        assert slots[0] == (float(ARTIFACT_GRID_X), 80.0)
        assert slots[1] == (float(ARTIFACT_GRID_X + ARTIFACT_COL_WIDTH), 80.0)
        assert slots[2] == (float(ARTIFACT_GRID_X + 2 * ARTIFACT_COL_WIDTH), 80.0)
        assert slots[3] == (float(ARTIFACT_GRID_X), 80.0 + ARTIFACT_ROW_HEIGHT)
        # Remove slot 1's node and confirm the freed cell is reused
        conn.execute(
            "DELETE FROM canvas_node WHERE canvas_id = 2 AND x = ? AND y = ?",
            slots[1],
        )
        conn.commit()
        nxt = next_artifact_slot(conn, 2)
        assert nxt == slots[1]
    finally:
        conn.close()


def test_tidy_rearranges_stacked_nodes(client):
    conn = _conn()
    try:
        conn.execute("INSERT INTO projects (id, title) VALUES (1, 'Tidy')")
        conn.execute("INSERT INTO canvas (id, project_id, title) VALUES (1, 1, 'c')")
        # Deliberately stacked garbage positions
        for i in range(6):
            conn.execute(
                "INSERT INTO canvas_node (canvas_id, type, entity_type, x, y) "
                "VALUES (1, 'entity', 'character', 5, 5)",
            )
        for i in range(4):
            conn.execute(
                "INSERT INTO canvas_node (canvas_id, type, x, y) VALUES (1, 'image', 7, 7)",
            )
        conn.commit()
    finally:
        conn.close()

    resp = client.post("/api/canvas/1/tidy")
    assert resp.status_code == 200
    body = resp.json()
    assert body["moved"] == 10

    conn = _conn()
    try:
        ents = conn.execute(
            "SELECT x, y FROM canvas_node WHERE canvas_id = 1 AND type = 'entity' ORDER BY y"
        ).fetchall()
        ys = [r["y"] for r in ents]
        assert all(b - a == ENTITY_ROW_HEIGHT for a, b in zip(ys, ys[1:]))
        assert all(r["x"] == ENTITY_COLUMNS["character"] for r in ents)
        arts = conn.execute(
            "SELECT x, y FROM canvas_node WHERE canvas_id = 1 AND type = 'image' ORDER BY id"
        ).fetchall()
        # Grid: 3 in row 0, 1 in row 1
        assert arts[3]["y"] == 80.0 + ARTIFACT_ROW_HEIGHT
        assert arts[0]["x"] == float(ARTIFACT_GRID_X)
    finally:
        conn.close()
