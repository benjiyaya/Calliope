"""The CLI's continuity board must be the same board the render path sees.

``agent.continuity.load_board`` opens its own connection
(``continuity.py:185``), so inside a write transaction it would read a
*committed* snapshot -- the rows just inserted would be invisible to it.
``authoring/context.py`` rebuilds the board from the caller's connection
instead, with a copy of the same SELECTs.

A copy is a liability: if the upstream SELECT gains a column, the copy goes
quietly stale and ``plan_state`` reports a different ``basis_hash`` than the
recalculation path. Then ``plan next`` says "current" for a plan the renderer
is about to recalculate, and the user's continuity work is dropped with no
error. These tests fail on that drift instead.
"""
from __future__ import annotations

import asyncio

import pytest

from calliope.agent.continuity import basis_hash, load_board as agent_load_board
from calliope.authoring.context import load_board as authoring_load_board


@pytest.fixture
def conn(tmp_path, monkeypatch):
    import calliope.config as config_module
    from calliope.db import get_db, migrate_db

    monkeypatch.setattr(config_module.settings, "data_dir", tmp_path)
    monkeypatch.setattr(config_module.settings, "assets_dir", tmp_path / "assets")
    monkeypatch.setattr(config_module.Settings, "save_config_file", lambda self: None)
    asyncio.run(migrate_db(config_module.settings.db_path))

    c = get_db(config_module.settings.db_path)
    c.isolation_level = None
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def populated(conn):
    """One project with every kind of row the board reads, plus an empty one."""
    pid = int(
        conn.execute(
            "INSERT INTO projects (title, idea, genre, tone) "
            "VALUES ('P', 'the novel text', 'noir', 'tense')"
        ).lastrowid
    )
    conn.execute(
        "INSERT INTO locations (project_id, name, description, consistency_prompt) "
        "VALUES (?, 'Hall', 'a dusty hall', 'warm light')",
        (pid,),
    )
    conn.execute(
        "INSERT INTO characters (project_id, name, appearance, consistency_prompt) "
        "VALUES (?, 'Ann', 'red coat', 'keep the coat red')",
        (pid,),
    )
    conn.execute("UPDATE characters SET sheet_path = 'sheet/ann.png' WHERE project_id = ?", (pid,))
    scene_id = int(
        conn.execute(
            "INSERT INTO scenes (project_id, order_index, heading, action, dialog, "
            "location_id, env_image_path) VALUES (?, 1, 'S1', 'she enters', "
            "'hello', 1, 'env/1.png')",
            (pid,),
        ).lastrowid
    )
    conn.execute(
        "INSERT INTO clips (project_id, scene_id, order_index, description, "
        "shot_size, duration_sec, dialog_lines_covered, clip_path) "
        "VALUES (?, ?, 1, 'wide of the hall', 'wide', 4.0, '[1]', 'out/1.mp4')",
        (pid, scene_id),
    )
    empty = int(
        conn.execute("INSERT INTO projects (title) VALUES ('Empty')").lastrowid
    )
    return pid, empty


def _columns(board):
    """Column names per bucket -- the thing that actually drifts.

    ``project`` is a single row, not a list; the rest are lists.
    """
    out = {}
    for bucket, rows in board.items():
        if isinstance(rows, dict):
            out[bucket] = sorted(rows)
        else:
            out[bucket] = sorted(rows[0]) if rows else []
    return out


def test_the_two_boards_have_the_same_columns(conn, populated):
    pid, _empty = populated
    assert _columns(authoring_load_board(conn, pid)) == _columns(agent_load_board(pid))


def test_the_two_boards_have_the_same_values(conn, populated):
    pid, _empty = populated
    assert authoring_load_board(conn, pid) == agent_load_board(pid)


def test_the_two_boards_hash_identically(conn, populated):
    """The load-bearing one. ``basis_hash`` is what decides current vs stale,
    so equal boards hashing differently would mean ``plan next`` and the
    renderer disagree about what changed."""
    pid, _empty = populated
    assert basis_hash(authoring_load_board(conn, pid)) == basis_hash(
        agent_load_board(pid)
    )


def test_the_top_level_buckets_match(conn, populated):
    pid, _empty = populated
    assert set(authoring_load_board(conn, pid)) == set(agent_load_board(pid))


def test_clip_columns_match_one_for_one(conn, populated):
    """``SELECT c.*, s.<5 columns>`` is the line most likely to be edited on one
    side only, and a column added to just one copy changes the hash."""
    pid, _empty = populated
    mine = authoring_load_board(conn, pid)["clips"][0]
    theirs = agent_load_board(pid)["clips"][0]
    assert set(mine) == set(theirs)


def test_an_empty_project_hashes_the_same(conn, populated):
    _pid, empty = populated
    assert basis_hash(authoring_load_board(conn, empty)) == basis_hash(
        agent_load_board(empty)
    )


def test_an_unknown_project_is_not_found_in_both(conn):
    from calliope.authoring.service import NotFound

    with pytest.raises(NotFound):
        authoring_load_board(conn, 999999)
    # The agent version raises a plain ValueError. Different exception type is
    # fine -- it is the same 404 from the caller's point of view, because the
    # CLI maps AuthoringError to exit 4 and never lets ValueError escape.
    with pytest.raises(ValueError):
        agent_load_board(999999)
