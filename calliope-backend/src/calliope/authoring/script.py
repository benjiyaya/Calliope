"""Script authoring: scenes and clips.

Three invariants here are not negotiable, and each is enforced by an existing
subsystem that reads the script as-is:

1. ``scenes.order_index`` is a dense 1..N sequence. ``video_agent.py:396``
   builds a render timeline from it, ``continuity.py:201`` orders clips by it,
   and ``export/runner.py:102`` slices on it. A gap means a hole in the film.
2. Every scene keeps >= 1 clip (``db.ensure_default_clip``, called by every
   scene-creation path; the router's 422 guard is ``scenes.py:387-391``).
3. ``scenes.location_id`` has NO foreign key (``db.py:77``). Nothing in the
   schema stops a dangling id, so it is checked here or the location silently
   vanishes from every downstream join.

Scene delete is exposed as ``replace-range`` only: the range is replaced in one
transaction so a single ``undo`` entry restores it. There is no per-scene
delete verb, same as beats.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Sequence

from calliope.authoring.models import ClipIn, ClipPatch, SceneIn, ScenePatch
from calliope.authoring.service import (
    NotFound,
    OrderingError,
    ValidationFailed,
)
from calliope.db import ensure_default_clip, row_to_dict

SCENE_FIELDS = (
    "heading",
    "action",
    "dialog",
    "duration_sec",
    "order_index",
    "location_id",
)
CLIP_FIELDS = (
    "description",
    "shot_size",
    "duration_sec",
    "order_index",
    "dialog_lines_covered",
)

# Clip fields the CLI may write. Everything else in ``clips`` is production
# state owned by the render pipeline: ``clip_path`` (generated video),
# ``workflow_id`` / ``video_settings_json`` (renderer config),
# ``chain_from_prev`` (drives image/video chaining).
CLI_CLIP_FIELDS = ("description", "shot_size", "duration_sec", "dialog_lines_covered")

#: Prefix marking a continuity constraint inside ``clips.description`` (plan
#: §4.1.1). ``context set`` deliberately has no ``shots`` field -- the plan is
#: rebuilt from scratch whenever ``based_on`` stops matching the board -- so a
#: per-clip constraint has exactly one writable home, and it is the line the
#: clip's description already carries to the renderer.
CARRY_PREFIX = "[CARRY]"


def parse_carry(description: Any) -> str:
    """The ``[CARRY]`` block of a clip description, or "" if it has none.

    The block is everything from the marker line to the end of the description.
    A constraint is a paragraph, not a sentence -- "the lantern stays lit" is
    almost never the whole rule, there is always a second clause about the
    coat or the eyeline. Stopping at the first newline would silently truncate
    it, and the caller would have no way to tell that had happened.

    Two marker lines are tolerated rather than rejected: the renderer reads the
    whole description, and someone adding a constraint mid-script should not
    have to delete the old one first. The first marker wins.
    """
    if not description:
        return ""
    lines = str(description).splitlines()
    for index, line in enumerate(lines):
        if line.startswith(CARRY_PREFIX):
            block = [line[len(CARRY_PREFIX) :].strip()]
            block.extend(rest.strip() for rest in lines[index + 1 :])
            return "\n".join(part for part in block if part)
    return ""


def carry_of(conn: sqlite3.Connection, project_id: int, clip_id: Any) -> str:
    """``parse_carry`` for one clip, read from the database.

    The ``project_id`` in the WHERE is load-bearing: a clip id is
    caller-supplied, so without it this would read across project boundaries.
    """
    if clip_id is None:
        return ""
    row = conn.execute(
        "SELECT description FROM clips WHERE id = ? AND project_id = ?",
        (int(clip_id), int(project_id)),
    ).fetchone()
    return parse_carry(row["description"]) if row else ""


def carry_lines(clips: Sequence[dict[str, Any]]) -> dict[Any, str]:
    """``{clip_id: carry text}`` for a list of clip rows already read.

    The read face has the descriptions in hand; re-querying per clip would be a
    second round trip per shot just to split a string.
    """
    out: dict[Any, str] = {}
    for clip in clips:
        carry = parse_carry(clip.get("description"))
        if carry:
            out[clip.get("id")] = carry
    return out


def undescribed_scenes(
    conn: sqlite3.Connection, project_id: int
) -> list[dict[str, Any]]:
    """Scenes whose clips are all still the auto-generated default.

    A scene always has at least one clip (``db.ensure_default_clip``), so
    "exists" is not the same as "written". The default row has no description,
    which makes ``description IS NOT NULL AND TRIM(...) <> ''`` the honest test
    for "someone has shot-listed this".
    """
    return [
        row_to_dict(r)
        for r in conn.execute(
            """
            SELECT s.id, s.order_index, s.heading
            FROM scenes s
            WHERE s.project_id = ?
              AND NOT EXISTS (
                SELECT 1 FROM clips c
                WHERE c.scene_id = s.id
                  AND c.description IS NOT NULL
                  AND TRIM(c.description) <> ''
              )
            ORDER BY s.order_index, s.id
            """,
            (int(project_id),),
        ).fetchall()
    ]


def get_scene(
    conn: sqlite3.Connection, project_id: int, scene_id: int
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM scenes WHERE id = ? AND project_id = ?",
        (int(scene_id), int(project_id)),
    ).fetchone()
    if row is None:
        raise NotFound(f"Scene {scene_id} not found in project {project_id}")
    out = row_to_dict(row)
    out["clips"] = list_clips(conn, int(scene_id))
    out["characters"] = _scene_character_names(conn, int(scene_id))
    return out


def _scene_character_names(conn: sqlite3.Connection, scene_id: int) -> list[str]:
    return [
        str(r["name"])
        for r in conn.execute(
            """
            SELECT c.name FROM scene_characters sc
            JOIN characters c ON c.id = sc.character_id
            WHERE sc.scene_id = ? ORDER BY c.name
            """,
            (int(scene_id),),
        ).fetchall()
    ]


def list_scenes(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    from_index: int | None = None,
    to_index: int | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM scenes WHERE project_id = ?"
    params: list[Any] = [int(project_id)]
    if from_index is not None:
        sql += " AND order_index >= ?"
        params.append(int(from_index))
    if to_index is not None:
        sql += " AND order_index <= ?"
        params.append(int(to_index))
    sql += " ORDER BY order_index, id"
    return [row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def list_clips(
    conn: sqlite3.Connection, scene_id: int
) -> list[dict[str, Any]]:
    return [
        row_to_dict(r)
        for r in conn.execute(
            "SELECT * FROM clips WHERE scene_id = ? ORDER BY order_index, id",
            (int(scene_id),),
        ).fetchall()
    ]


def list_all_clips(
    conn: sqlite3.Connection, project_id: int
) -> list[dict[str, Any]]:
    """Clips with their scene fields joined, in render order.

    Mirrors the ``load_board`` clip SELECT (``continuity.py:196-205``) so a
    caller sees exactly what the continuity hash will see.
    """
    return [
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


def _check_location(
    conn: sqlite3.Connection, project_id: int, location_id: Any
) -> None:
    """``scenes.location_id`` has no FK (``db.py:77``) -- verify it here."""
    if location_id is None:
        return
    row = conn.execute(
        "SELECT id FROM locations WHERE id = ? AND project_id = ?",
        (int(location_id), int(project_id)),
    ).fetchone()
    if row is None:
        raise ValidationFailed(
            f"location {location_id} is not a location of project {project_id}",
            [{"loc": "location_id", "msg": "no such location in this project"}],
        )


def _resolve_characters(
    conn: sqlite3.Connection, project_id: int, names: Sequence[str]
) -> list[int]:
    if not names:
        return []
    ids: list[int] = []
    for name in names:
        row = conn.execute(
            "SELECT id FROM characters WHERE project_id = ? AND name = ? ORDER BY id",
            (int(project_id), name),
        ).fetchone()
        if row is None:
            raise ValidationFailed(
                f"character {name!r} is not in project {project_id}",
                [
                    {
                        "loc": "characters",
                        "msg": (
                            f"unknown character {name!r}; write it with "
                            "`cast upsert --kind character` first"
                        ),
                    }
                ],
            )
        ids.append(int(row["id"]))
    return ids


def _renumber_scenes(
    conn: sqlite3.Connection, project_id: int
) -> None:
    """Force ``scenes.order_index`` back to a dense 1..N.

    Called after any structural scene write. Renumbering is what keeps the
    render timeline gapless no matter what the caller asked for; the caller's
    explicit ``order_index`` only decides the *sequence*, not the final numbers.
    """
    rows = conn.execute(
        "SELECT id FROM scenes WHERE project_id = ? ORDER BY order_index, id",
        (int(project_id),),
    ).fetchall()
    for position, row in enumerate(rows, start=1):
        conn.execute(
            "UPDATE scenes SET order_index = ? WHERE id = ?",
            (position, int(row["id"])),
        )


def append_scenes(
    conn: sqlite3.Connection, project_id: int, scenes: Sequence[SceneIn]
) -> list[int]:
    """Append scenes after the current tail, then renumber to dense 1..N."""
    tail = conn.execute(
        "SELECT COALESCE(MAX(order_index), 0) AS m FROM scenes WHERE project_id = ?",
        (int(project_id),),
    ).fetchone()["m"]
    cursor = int(tail) + 1
    ids: list[int] = []
    for scene in scenes:
        order = scene.order_index if scene.order_index is not None else cursor
        if order < 1:
            raise OrderingError(f"scene order_index must be >= 1, got {order}")
        _check_location(conn, project_id, scene.location_id)
        cur = conn.execute(
            """
            INSERT INTO scenes (project_id, order_index, heading, action, dialog,
                                duration_sec, location_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(project_id),
                order,
                scene.heading,
                scene.action,
                scene.dialog,
                scene.duration_sec,
                scene.location_id,
            ),
        )
        scene_id = int(cur.lastrowid)
        _link_characters(conn, project_id, scene_id, scene.characters)
        ensure_default_clip(conn, scene_id, int(project_id))
        ids.append(scene_id)
        cursor = max(cursor, order) + 1
    _renumber_scenes(conn, project_id)
    return ids


def _link_characters(
    conn: sqlite3.Connection, project_id: int, scene_id: int, names: Sequence[str]
) -> None:
    ids = _resolve_characters(conn, project_id, names)
    if not ids:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO scene_characters (scene_id, character_id) VALUES (?, ?)",
        [(int(scene_id), cid) for cid in ids],
    )


def update_scene(
    conn: sqlite3.Connection,
    project_id: int,
    scene_id: int,
    patch: ScenePatch,
) -> dict[str, Any]:
    get_scene(conn, project_id, scene_id)  # 404 first
    fields = {
        k: v for k, v in patch.model_dump(exclude_unset=True).items() if v is not None
    }
    if not fields:
        return get_scene(conn, project_id, scene_id)
    if "location_id" in fields:
        _check_location(conn, project_id, fields["location_id"])
    unknown = set(fields) - set(SCENE_FIELDS)
    if unknown:
        raise ValidationFailed(
            f"scene has no column(s) {sorted(unknown)}; writable: {list(SCENE_FIELDS)}",
            [{"loc": k, "msg": "not a scenes column"} for k in sorted(unknown)],
        )
    if "order_index" in fields:
        # A move is a reorder of the whole sequence, not a hole: shift the
        # others aside, then let the renumber settle the final numbering.
        _move_scene(conn, project_id, scene_id, int(fields["order_index"]))
        fields.pop("order_index")
    sets = ", ".join(f"{k} = ?" for k in fields)
    if fields:
        conn.execute(
            f"UPDATE scenes SET {sets} WHERE id = ? AND project_id = ?",
            (*fields.values(), int(scene_id), int(project_id)),
        )
    _renumber_scenes(conn, project_id)
    return get_scene(conn, project_id, scene_id)


def _move_scene(
    conn: sqlite3.Connection, project_id: int, scene_id: int, to_order: int
) -> None:
    """Reposition one scene inside the 1..N sequence."""
    if to_order < 1:
        raise OrderingError(f"order_index must be >= 1, got {to_order}")
    rows = conn.execute(
        "SELECT id, order_index FROM scenes WHERE project_id = ? ORDER BY order_index, id",
        (int(project_id),),
    ).fetchall()
    order = [int(r["id"]) for r in rows]
    if int(scene_id) not in order:
        raise NotFound(f"Scene {scene_id} not found in project {project_id}")
    order.remove(int(scene_id))
    order.insert(min(to_order, len(order)) - 1, int(scene_id))
    conn.executemany(
        "UPDATE scenes SET order_index = ? WHERE id = ?",
        list(enumerate(order, start=1)),
    )


def replace_scene_range(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    from_index: int,
    to_index: int,
    scenes: Sequence[SceneIn],
) -> dict[str, Any]:
    """Replace scenes ``from_index..to_index`` with ``scenes``.

    Clips of the removed scenes go with them (``clips.scene_id`` is
    ``ON DELETE CASCADE``, ``db.py:95``). The removed clip rows are returned so
    the audit entry -- and therefore ``undo`` -- keeps them.
    """
    if from_index < 1 or to_index < from_index:
        raise OrderingError(
            f"invalid range {from_index}..{to_index}; expected 1 <= from <= to"
        )
    existing = list_scenes(conn, project_id)
    removed = [
        s for s in existing if from_index <= int(s["order_index"]) <= to_index
    ]
    if not removed:
        raise NotFound(
            f"No scenes in range {from_index}..{to_index} for project {project_id}"
        )
    removed_clips = [
        clip for scene in removed for clip in list_clips(conn, int(scene["id"]))
    ]
    removed_characters = {
        int(scene["id"]): _scene_character_names(conn, int(scene["id"]))
        for scene in removed
    }
    conn.executemany(
        "DELETE FROM scenes WHERE id = ? AND project_id = ?",
        [(int(s["id"]), int(project_id)) for s in removed],
    )
    new_ids: list[int] = []
    for offset, scene in enumerate(scenes):
        _check_location(conn, project_id, scene.location_id)
        cur = conn.execute(
            """
            INSERT INTO scenes (project_id, order_index, heading, action, dialog,
                                duration_sec, location_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(project_id),
                from_index + offset,
                scene.heading,
                scene.action,
                scene.dialog,
                scene.duration_sec,
                scene.location_id,
            ),
        )
        scene_id = int(cur.lastrowid)
        _link_characters(conn, project_id, scene_id, scene.characters)
        ensure_default_clip(conn, scene_id, int(project_id))
        new_ids.append(scene_id)
    _renumber_scenes(conn, project_id)
    return {
        "removed": removed,
        "removed_clips": removed_clips,
        "removed_characters": removed_characters,
        "added_ids": new_ids,
        "range": [from_index, to_index],
    }


# --------------------------------------------------------------------------
# clips
# --------------------------------------------------------------------------


def get_clip(
    conn: sqlite3.Connection, project_id: int, clip_id: int
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM clips WHERE id = ? AND project_id = ?",
        (int(clip_id), int(project_id)),
    ).fetchone()
    if row is None:
        raise NotFound(f"Clip {clip_id} not found in project {project_id}")
    return row_to_dict(row)


def _encode_lines(value: Any) -> str | None:
    """``dialog_lines_covered`` is TEXT holding a JSON array (``coverage_agent.py:288``)."""
    if value is None:
        return None
    return json.dumps(value)


def append_clips(
    conn: sqlite3.Connection,
    project_id: int,
    scene_id: int,
    clips: Sequence[ClipIn],
) -> list[int]:
    scene = conn.execute(
        "SELECT id FROM scenes WHERE id = ? AND project_id = ?",
        (int(scene_id), int(project_id)),
    ).fetchone()
    if scene is None:
        raise NotFound(f"Scene {scene_id} not found in project {project_id}")
    cursor = (
        conn.execute(
            "SELECT COALESCE(MAX(order_index), 0) AS m FROM clips WHERE scene_id = ?",
            (int(scene_id),),
        ).fetchone()["m"]
    )
    cursor = int(cursor) + 1
    ids: list[int] = []
    for clip in clips:
        order = clip.order_index if clip.order_index is not None else cursor
        if order < 1:
            raise OrderingError(f"clip order_index must be >= 1, got {order}")
        cur = conn.execute(
            """
            INSERT INTO clips (scene_id, project_id, order_index, description,
                               shot_size, duration_sec, dialog_lines_covered)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(scene_id),
                int(project_id),
                order,
                clip.description,
                clip.shot_size,
                clip.duration_sec,
                _encode_lines(clip.dialog_lines_covered),
            ),
        )
        ids.append(int(cur.lastrowid))
        cursor = max(cursor, order) + 1
    _renumber_clips(conn, int(scene_id))
    return ids


def _renumber_clips(conn: sqlite3.Connection, scene_id: int) -> None:
    """Dense 1..N within the scene -- ``continuity.py:201`` orders on it."""
    rows = conn.execute(
        "SELECT id FROM clips WHERE scene_id = ? ORDER BY order_index, id",
        (int(scene_id),),
    ).fetchall()
    for position, row in enumerate(rows, start=1):
        conn.execute(
            "UPDATE clips SET order_index = ? WHERE id = ?",
            (position, int(row["id"])),
        )


def update_clip(
    conn: sqlite3.Connection, project_id: int, clip_id: int, patch: ClipPatch
) -> dict[str, Any]:
    clip = get_clip(conn, project_id, clip_id)
    fields = dict(patch.model_dump(exclude_unset=True))
    new_order = fields.pop("order_index", None)
    unknown = set(fields) - set(CLI_CLIP_FIELDS)
    if unknown:
        raise ValidationFailed(
            f"the CLI may not write clip column(s) {sorted(unknown)}; "
            f"authorable: {list(CLI_CLIP_FIELDS)}. Render state (clip_path, "
            "workflow_id, video_settings_json, chain_from_prev) belongs to the "
            "web UI.",
            [{"loc": k, "msg": "not authorable through the CLI"} for k in sorted(unknown)],
        )
    effective = dict(fields)
    if "dialog_lines_covered" in effective:
        effective["dialog_lines_covered"] = _encode_lines(
            effective["dialog_lines_covered"]
        )
    changed = {k: v for k, v in effective.items() if clip[k] != v}
    if changed:
        sets = ", ".join(f"{k} = ?" for k in changed)
        conn.execute(
            f"UPDATE clips SET {sets} WHERE id = ? AND project_id = ?",
            (*changed.values(), int(clip_id), int(project_id)),
        )
    if new_order is not None:
        _move_clip(conn, int(clip["scene_id"]), int(clip_id), int(new_order))
    _renumber_clips(conn, int(clip["scene_id"]))
    return get_clip(conn, project_id, clip_id)


def _move_clip(
    conn: sqlite3.Connection, scene_id: int, clip_id: int, to_order: int
) -> None:
    if to_order < 1:
        raise OrderingError(f"order_index must be >= 1, got {to_order}")
    rows = conn.execute(
        "SELECT id FROM clips WHERE scene_id = ? ORDER BY order_index, id",
        (int(scene_id),),
    ).fetchall()
    order = [int(r["id"]) for r in rows]
    if int(clip_id) not in order:
        raise NotFound(f"Clip {clip_id} not in scene {scene_id}")
    order.remove(int(clip_id))
    order.insert(min(to_order, len(order)) - 1, int(clip_id))
    conn.executemany(
        "UPDATE clips SET order_index = ? WHERE id = ?",
        list(enumerate(order, start=1)),
    )


def replace_clip_range(
    conn: sqlite3.Connection,
    project_id: int,
    scene_id: int,
    *,
    from_index: int,
    to_index: int,
    clips: Sequence[ClipIn],
) -> dict[str, Any]:
    """Replace clips ``from_index..to_index`` inside one scene.

    The replacement is never allowed to be empty: a scene with no clips is
    rejected by the UI with a 422 (``scenes.py:387-391``), so producing one here
    would write a state the rest of the app refuses to accept.
    """
    if not clips:
        raise ValidationFailed(
            "clips replace-range needs at least one replacement clip; a scene "
            "must keep >= 1 clip",
            [{"loc": "$", "msg": "empty replacement would leave the scene with no clip"}],
        )
    if from_index < 1 or to_index < from_index:
        raise OrderingError(
            f"invalid range {from_index}..{to_index}; expected 1 <= from <= to"
        )
    existing = list_clips(conn, scene_id)
    removed = [c for c in existing if from_index <= int(c["order_index"]) <= to_index]
    if not removed:
        raise NotFound(
            f"No clips in range {from_index}..{to_index} for scene {scene_id}"
        )
    conn.executemany(
        "DELETE FROM clips WHERE id = ?",
        [(int(c["id"]),) for c in removed],
    )
    new_ids: list[int] = []
    for offset, clip in enumerate(clips):
        cur = conn.execute(
            """
            INSERT INTO clips (scene_id, project_id, order_index, description,
                               shot_size, duration_sec, dialog_lines_covered)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(scene_id),
                int(project_id),
                from_index + offset,
                clip.description,
                clip.shot_size,
                clip.duration_sec,
                _encode_lines(clip.dialog_lines_covered),
            ),
        )
        new_ids.append(int(cur.lastrowid))
    _renumber_clips(conn, scene_id)
    return {
        "removed": removed,
        "added_ids": new_ids,
        "range": [from_index, to_index],
        "scene_id": int(scene_id),
    }


def assert_invariants(conn: sqlite3.Connection, project_id: int) -> None:
    """Post-write invariant check. Called by the CLI after every write.

    Cheap (three grouped queries) and it turns "the CLI corrupted the script"
    from a silent rendering bug into a loud failure on the write that caused it.
    """
    dense = conn.execute(
        """
        SELECT COUNT(*) AS n, COUNT(DISTINCT order_index) AS d,
               MIN(order_index) AS lo, MAX(order_index) AS hi
        FROM scenes WHERE project_id = ?
        """,
        (int(project_id),),
    ).fetchone()
    if dense["n"] and (dense["n"] != dense["d"] or int(dense["lo"]) != 1):
        raise OrderingError(
            f"scene order_index is not a dense 1..N sequence "
            f"({dense['n']} rows, {dense['d']} distinct, min {dense['lo']})"
        )
    orphan = conn.execute(
        """
        SELECT c.id, c.scene_id FROM clips c
        LEFT JOIN scenes s ON s.id = c.scene_id
        WHERE c.project_id = ? AND s.id IS NULL LIMIT 5
        """,
        (int(project_id),),
    ).fetchall()
    if orphan:
        raise OrderingError(
            "clips without a scene after write: "
            + ", ".join(f"clip {r['id']}->scene {r['scene_id']}" for r in orphan)
        )
    empty = conn.execute(
        """
        SELECT s.id, s.order_index FROM scenes s
        WHERE s.project_id = ?
          AND NOT EXISTS (SELECT 1 FROM clips c WHERE c.scene_id = s.id)
        LIMIT 5
        """,
        (int(project_id),),
    ).fetchall()
    if empty:
        raise OrderingError(
            "scenes left with no clip after write: "
            + ", ".join(f"scene {r['id']} (#{r['order_index']})" for r in empty)
        )