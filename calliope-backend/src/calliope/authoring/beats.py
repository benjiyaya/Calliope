"""Story beat authoring.

``replace-range`` is the only shape that deletes, and it is deliberately a
single audited operation: the range is removed and the replacement inserted in
one transaction, so one ``undo`` entry restores the whole thing.

``story_beats.order_index`` is a soft invariant in the existing code -- holes
do not raise (``story.py:541`` does not renumber). We renumber on write anyway
so ``plan`` can report a dense range, but we do not reject a caller's explicit
gaps at read time.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Sequence

from calliope.authoring.models import BeatIn, BeatPatch
from calliope.authoring.service import (
    NotFound,
    OrderingError,
    next_order_index,
)
from calliope.db import row_to_dict

FIELDS = ("title", "description", "order_index")


def get_beat(conn: sqlite3.Connection, project_id: int, beat_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM story_beats WHERE id = ? AND project_id = ?",
        (int(beat_id), int(project_id)),
    ).fetchone()
    if row is None:
        raise NotFound(f"Beat {beat_id} not found in project {project_id}")
    return row_to_dict(row)


def list_beats(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    from_index: int | None = None,
    to_index: int | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM story_beats WHERE project_id = ?"
    params: list[Any] = [int(project_id)]
    if from_index is not None:
        sql += " AND order_index >= ?"
        params.append(int(from_index))
    if to_index is not None:
        sql += " AND order_index <= ?"
        params.append(int(to_index))
    sql += " ORDER BY order_index, id"
    return [row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def _validate_order(
    conn: sqlite3.Connection, project_id: int, order_index: int
) -> None:
    """order_index must be >= 1. There is no upper bound: the sequence is
    extended as beats are appended, so a gap at the tail is legal."""
    if order_index < 1:
        raise OrderingError(
            f"order_index must be >= 1, got {order_index} "
            "(the UI and the exporters both treat 1 as the first beat)"
        )


def append_beats(
    conn: sqlite3.Connection, project_id: int, beats: Sequence[BeatIn]
) -> list[int]:
    ids: list[int] = []
    cursor = next_order_index(conn, "story_beats", project_id)
    for beat in beats:
        order = beat.order_index if beat.order_index is not None else cursor
        _validate_order(conn, project_id, order)
        cur = conn.execute(
            """
            INSERT INTO story_beats (project_id, order_index, title, description)
            VALUES (?, ?, ?, ?)
            """,
            (int(project_id), order, beat.title, beat.description),
        )
        ids.append(int(cur.lastrowid))
        cursor = max(cursor, order) + 1
    return ids


def update_beat(
    conn: sqlite3.Connection,
    project_id: int,
    beat_id: int,
    patch: BeatPatch,
) -> dict[str, Any]:
    before = get_beat(conn, project_id, beat_id)
    fields = {
        k: v
        for k, v in patch.model_dump(exclude_unset=True).items()
        if v is not None
    }
    if not fields:
        return before
    if "order_index" in fields:
        _validate_order(conn, project_id, int(fields["order_index"]))
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE story_beats SET {sets} WHERE id = ? AND project_id = ?",
        (*fields.values(), int(beat_id), int(project_id)),
    )
    return get_beat(conn, project_id, beat_id)


def replace_range(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    from_index: int,
    to_index: int,
    beats: Sequence[BeatIn],
) -> dict[str, Any]:
    """Replace beats ``from_index..to_index`` with ``beats``.

    Returns the removed rows and the new ids so the caller can build one audit
    entry. Nothing is committed here.
    """
    if from_index < 1 or to_index < from_index:
        raise OrderingError(
            f"invalid range {from_index}..{to_index}; expected 1 <= from <= to"
        )
    existing = list_beats(conn, project_id)
    removed = [b for b in existing if from_index <= int(b["order_index"]) <= to_index]
    if not removed:
        raise NotFound(
            f"No beats in range {from_index}..{to_index} for project {project_id}"
        )
    tail = [
        b
        for b in existing
        if int(b["order_index"]) > to_index
    ]
    conn.executemany(
        "DELETE FROM story_beats WHERE id = ? AND project_id = ?",
        [(int(b["id"]), int(project_id)) for b in removed],
    )
    new_ids: list[int] = []
    for offset, beat in enumerate(beats):
        order = from_index + offset
        cur = conn.execute(
            """
            INSERT INTO story_beats (project_id, order_index, title, description)
            VALUES (?, ?, ?, ?)
            """,
            (int(project_id), order, beat.title, beat.description),
        )
        new_ids.append(int(cur.lastrowid))
    shift = len(beats) - len(removed)
    if shift:
        for b in tail:
            conn.execute(
                "UPDATE story_beats SET order_index = order_index + ? WHERE id = ?",
                (shift, int(b["id"])),
            )
    return {
        "removed": removed,
        "added_ids": new_ids,
        "shift": shift,
        "range": [from_index, to_index],
    }
