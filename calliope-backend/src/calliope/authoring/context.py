"""Continuity board built from an existing transaction.

Why this module exists. ``agent.continuity.load_board`` opens its own
connection (``continuity.py:185``). Inside our write transaction that would
read a *committed* snapshot -- the rows we just inserted are invisible to it.
So the board is rebuilt here from the caller's ``conn``, using the exact same
SELECT statements and column set.

What we deliberately do NOT reimplement is the semantics: ``basis_hash`` and
``deterministic_plan`` are imported from ``agent.continuity`` and stay the one
definition of "what a stale plan means". A second copy of those would drift,
and a plan whose hash disagrees with the recalculation path is worthless.

``ensure_continuity_plan`` is NOT imported: it calls the LLM. ``plan next``
reads what is stored and reports staleness; it never refreshes (plan §5.7).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from calliope.agent.continuity import basis_hash, deterministic_plan
from calliope.authoring.service import NotFound
from calliope.db import row_to_dict


def load_board(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    """Same shape as ``continuity.load_board``, same connection, no commit.

    Column-for-column identical to ``continuity.py:193-225`` on purpose. If the
    upstream SELECT changes, this one has to change with it -- ``test_board_
    parity`` pins that.
    """
    project = conn.execute(
        "SELECT id, title, idea, genre, tone, continuity_json FROM projects WHERE id = ?",
        (int(project_id),),
    ).fetchone()
    if not project:
        raise NotFound(f"Project {project_id} not found")
    clips = [
        row_to_dict(r)
        for r in conn.execute(
            """
            SELECT c.*, s.order_index AS scene_order, s.heading, s.action, s.dialog,
                   s.location_id, s.env_image_path
            FROM clips c JOIN scenes s ON s.id = c.scene_id
            WHERE c.project_id = ?
            ORDER BY s.order_index, c.order_index, c.id
            """,
            (int(project_id),),
        ).fetchall()
    ]
    characters = [
        row_to_dict(r)
        for r in conn.execute(
            """
            SELECT id, name, appearance, consistency_prompt, sheet_path, portrait_path
            FROM characters WHERE project_id = ? ORDER BY id
            """,
            (int(project_id),),
        ).fetchall()
    ]
    locations = [
        row_to_dict(r)
        for r in conn.execute(
            """
            SELECT id, name, description, consistency_prompt, reference_image_path
            FROM locations WHERE project_id = ? ORDER BY id
            """,
            (int(project_id),),
        ).fetchall()
    ]
    return {
        "project": row_to_dict(project),
        "clips": clips,
        "characters": characters,
        "locations": locations,
    }


def stored_plan(project_row: dict[str, Any]) -> dict[str, Any] | None:
    """Parse ``projects.continuity_json`` the same way ``_parse_stored`` does.

    Mirrors ``continuity.py:370-379`` including the "shots must be a list"
    requirement -- a half-written blob that the recalculation path would treat
    as absent must be absent for us too, or ``plan`` would report a stale state
    that the UI would silently overwrite.
    """
    raw = project_row.get("continuity_json")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("shots"), list):
        return None
    return data


def load_stored_plan(
    conn: sqlite3.Connection, project_id: int
) -> dict[str, Any] | None:
    """The stored plan for one project, or ``None`` if there is no usable one.

    Exists so ``context set`` can capture a before-image without writing its own
    SELECT -- ``cli/`` holds no SQL (plan §3.1), and a caller that reaches for
    ``conn.execute`` to get this is how the layer gets eroded.
    """
    row = conn.execute(
        "SELECT id, continuity_json FROM projects WHERE id = ?", (int(project_id),)
    ).fetchone()
    if row is None:
        raise NotFound(f"Project {project_id} not found")
    return stored_plan(row_to_dict(row))


def plan_state(
    conn: sqlite3.Connection, project_id: int
) -> dict[str, Any]:
    """Report what the stored plan says, and whether it is current.

    Two different questions, deliberately not collapsed into one:

    ``authored`` -- does the ledger carry *writable* content (``overview`` /
    ``requirements``)? That is the CLI's half, and it is the thing a caller
    asking "what do I write next?" is really asking about.

    ``stale`` -- does ``stored.based_on`` still equal ``basis_hash(board)``?
    That tracks the *derived* ``shots``, which only ``ensure_continuity_plan``
    computes. The CLI cannot make this false, and pretending otherwise would
    make ``plan next`` unreachable: it would report ``context.set`` forever.

    Gating on ``authored`` and merely reporting ``stale`` keeps both honest --
    the writer stops when its own work is done, and the model-owned half stays
    visibly the model's to refresh.
    """
    board = load_board(conn, project_id)
    basis = basis_hash(board)
    stored = stored_plan(board["project"])
    if stored is None:
        state = "missing"
        stale = True
    else:
        stale = stored.get("based_on") != basis
        state = "stale" if stale else "current"
    authored = bool(
        stored
        and ((stored.get("overview") or {}) or (stored.get("requirements") or {}))
    )
    return {
        "state": state,
        "stale": stale,
        "authored": authored,
        "basis_hash": basis,
        "stored_based_on": (stored or {}).get("based_on") or "",
        "plan": stored or deterministic_plan(board),
        "board": board,
    }


def set_context(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    overview: dict[str, str],
    requirements: dict[str, str],
) -> dict[str, Any]:
    """Write the durable half of the continuity ledger.

    Only ``overview`` and ``requirements`` are writable. ``shots`` is left
    alone because it is a derived artifact: ``ensure_continuity_plan`` rebuilds
    every shot whenever ``based_on != basis_hash(board)``
    (``continuity.py:492``) and ``normalize_plan`` never reads the previous
    value (``continuity.py:336-347``). Per-clip constraints are authored on
    ``clips.description`` with a ``[CARRY]`` prefix instead (plan §4.1.1).

    Preserves ``shots``/``based_on`` from whatever is stored so a metadata-only
    write does not look like "the plan is gone" to the UI.

    Returns the merged document, so a caller that needs the previous value --
    ``context set`` records the derived half's before-image in its audit entry --
    gets it here rather than reaching for its own SELECT.
    """
    project = conn.execute(
        "SELECT id, continuity_json FROM projects WHERE id = ?", (int(project_id),)
    ).fetchone()
    if project is None:
        raise NotFound(f"Project {project_id} not found")
    existing = stored_plan(row_to_dict(project)) or {}
    merged = {
        "overview": overview,
        "requirements": requirements,
        "shots": existing.get("shots") or [],
        "based_on": existing.get("based_on") or "",
    }
    conn.execute(
        "UPDATE projects SET continuity_json = ? WHERE id = ?",
        (json.dumps(merged, ensure_ascii=False), int(project_id)),
    )
    return merged
