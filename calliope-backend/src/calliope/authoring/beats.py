"""Story beat authoring.

``replace-range`` is the only shape that deletes, and it is deliberately a
single audited operation: the range is removed and the replacement inserted in
one transaction, so one ``undo`` entry restores the whole thing.

``story_beats.order_index`` is a soft invariant in the existing code -- holes
do not raise (``story.py:541`` does not renumber). We renumber on write anyway
so ``plan`` can report a dense range, but we do not reject a caller's explicit
gaps at read time. A gap therefore survives a write without complaint, so
:func:`order_report` exists to *say so* on demand; nothing calls it implicitly,
because a soft invariant that suddenly raises would break the UI's own editors.
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


def order_report(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    """Describe the ``story_beats.order_index`` sequence without judging it.

    A report, not an assertion. ``scenes`` and ``clips`` are renumbered to a
    dense 1..N on every write and a write that breaks that raises; beats are not,
    because the existing web UI is allowed to leave gaps (``story.py:541`` does
    not renumber) and a soft invariant that suddenly became a hard one would
    break the user's own editor. The cost of that tolerance is that a gap can
    otherwise go unnoticed forever -- this is the detector that was missing.

    Reports both failure shapes, which need different fixes:

    * a **duplicate** means two rows claim one position, and the read order
      between them is ``id``, i.e. arbitrary from the caller's point of view.
      ``story append`` with an explicit occupied ``order_index`` does this.
    * a **hole** means a position in 1..max is unused. Harmless for reading
      (everything sorts by ``order_index, id``) but it means "replace beat 7"
      and "beat 7" are not the same row, which is a real trap for a caller.

    ``counts`` is the cheap form (``story list`` reports the same numbers), and
    ``dense`` is the one-word answer to "is anything wrong".
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS n, COUNT(DISTINCT order_index) AS d,
               MIN(order_index) AS lo, MAX(order_index) AS hi
        FROM story_beats WHERE project_id = ?
        """,
        (int(project_id),),
    ).fetchone()
    n, distinct = int(row["n"]), int(row["d"])
    lo = int(row["lo"]) if row["lo"] is not None else None
    hi = int(row["hi"]) if row["hi"] is not None else None
    dense = bool(n) and n == distinct and lo == 1 and hi == n

    duplicates: list[dict[str, Any]] = []
    holes: list[int] = []
    if n and not dense:
        for dup in conn.execute(
            """
            SELECT order_index, COUNT(*) AS c FROM story_beats
            WHERE project_id = ? GROUP BY order_index HAVING c > 1
            ORDER BY order_index
            """,
            (int(project_id),),
        ).fetchall():
            ids = [
                int(r["id"])
                for r in conn.execute(
                    "SELECT id FROM story_beats WHERE project_id = ? AND order_index = ?"
                    " ORDER BY id",
                    (int(project_id), int(dup["order_index"])),
                ).fetchall()
            ]
            duplicates.append({"order_index": int(dup["order_index"]), "ids": ids})
        if lo and hi:
            taken = {
                int(r["order_index"])
                for r in conn.execute(
                    "SELECT DISTINCT order_index FROM story_beats WHERE project_id = ?",
                    (int(project_id),),
                ).fetchall()
            }
            holes = [i for i in range(lo, hi + 1) if i not in taken]

    return {
        "counts": {"rows": n, "distinct": distinct, "min": lo, "max": hi},
        "dense": dense,
        "duplicates": duplicates,
        "holes": holes,
    }
