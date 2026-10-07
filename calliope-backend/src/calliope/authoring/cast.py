"""Cast authoring: characters / locations / items.

The three tables share ``id, project_id, name, ...`` but nothing else. The
differences are load-bearing, not cosmetic:

- ``characters`` has ``role, age, appearance, personality, portrait_path,
  sheet_path`` and is the only table ``load_board`` reads characters from
  (``continuity.py:206-215``), so character ``appearance`` /
  ``consistency_prompt`` feeds the continuity hash.
- ``locations`` has ``description, reference_image_path`` and is the only place
  ``basis_hash`` reads places from.
- ``items`` is not read by continuity at all.

So ``kind`` is resolved to a real table and a per-table allowlist, rather than
one merged field set that would silently write ``role`` onto a location.

Upsert is keyed on ``name`` (the identity the rest of the system uses --
``basis_hash`` compares names, the UI matches on them). A row that exists is
patched, not duplicated.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Sequence

from calliope.authoring.models import CastIn, CastPatch
from calliope.authoring.service import NotFound, ValidationFailed
from calliope.db import row_to_dict

# Per-table writable columns. Kept as explicit allowlists so the Pydantic
# models can stay generic (``CastPatch`` covers the union) while SQL never
# receives a column the table lacks.
#: Exactly the columns each table has (db.py:32-61). ``characters`` has no
#: ``description`` and no ``reference_image_path`` -- it uses ``appearance`` and
#: ``portrait_path``/``sheet_path`` instead. Getting this wrong is not a
#: cosmetic bug: a typo in a column name is an OperationalError at write time,
#: and a wrong-but-existing name silently writes to the wrong field.
WRITABLE: dict[str, tuple[str, ...]] = {
    "characters": (
        "name",
        "role",
        "age",
        "appearance",
        "personality",
        "consistency_prompt",
        "portrait_path",
        "sheet_path",
    ),
    "locations": (
        "name",
        "description",
        "consistency_prompt",
        "reference_image_path",
    ),
    "items": (
        "name",
        "description",
        "consistency_prompt",
        "reference_image_path",
    ),
}

#: CastIn.kind -> table. ``character`` is the default kind.
KIND_TABLE = {
    "character": "characters",
    "location": "locations",
    "item": "items",
}
TABLE_KIND = {v: k for k, v in KIND_TABLE.items()}

# Read-only for the CLI: these are rendered/generated output, not authored
# content. ``sheet_path`` and ``portrait_path`` in particular point at images
# the user generated in the UI; the CLI has no renderer and must not invent
# them. Allowed in a *patch* only to clear a stale reference (value "").
GENERATED_COLUMNS = frozenset(
    {"portrait_path", "sheet_path", "consistency_prompt"}
)


def _resolve(
    conn: sqlite3.Connection, table: str, project_id: int, ident: int | str
) -> tuple[int, sqlite3.Row]:
    """Resolve ``ident`` as a row id, or failing that as an exact name.

    Names are what a caller actually reads out of ``cast list``, so requiring an
    id first would make the common case two lookups. The id branch is tried
    first because it is exact and a name can never be an integer -- so the two
    can never collide, and there is nothing to disambiguate.
    """
    row = None
    if isinstance(ident, int) or str(ident).lstrip("-").isdigit():
        row = conn.execute(
            f"SELECT * FROM {table} WHERE id = ? AND project_id = ?",
            (int(ident), int(project_id)),
        ).fetchone()
    if row is None:
        row = _find_by_name(conn, table, project_id, str(ident))
    if row is None:
        raise NotFound(f"{table[:-1].title()} {ident!r} not found in project {project_id}")
    return int(row["id"]), row


def get_entity(
    conn: sqlite3.Connection, project_id: int, kind: str, entity_id: int | str
) -> dict[str, Any]:
    """Read one cast row. ``entity_id`` is a row id or the exact name."""
    _row_id, row = _resolve(conn, KIND_TABLE[kind], project_id, entity_id)
    out = row_to_dict(row)
    out["kind"] = kind
    return out


def list_cast(
    conn: sqlite3.Connection, project_id: int
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for table in ("characters", "locations", "items"):
        rows = [
            row_to_dict(r)
            for r in conn.execute(
                f"SELECT * FROM {table} WHERE project_id = ? ORDER BY id",
                (int(project_id),),
            ).fetchall()
        ]
        for row in rows:
            row["kind"] = TABLE_KIND[table]
        out[table] = rows
    return out


def _find_by_name(
    conn: sqlite3.Connection, table: str, project_id: int, name: str
) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT * FROM {table} WHERE project_id = ? AND name = ? ORDER BY id",
        (int(project_id), name),
    ).fetchone()


def _reject_unsupported(
    table: str, fields: dict[str, Any], kind: str, *, allow_name: bool = True
) -> None:
    """Fail loudly on a field the target table does not have.

    Silently dropping it would be the worst outcome: the caller reads a success,
    the field never lands, and the missing character detail only surfaces later
    as an off-model render.
    """
    unknown = set(fields) - set(WRITABLE[table])
    if not allow_name:
        unknown = unknown | ({"name"} & set(fields))
    if unknown:
        raise ValidationFailed(
            f"{kind} has no column(s) {sorted(unknown)}; "
            f"writable: {list(WRITABLE[table])}",
            [{"loc": k, "msg": f"not a {table} column"} for k in sorted(unknown)],
        )


def upsert(
    conn: sqlite3.Connection, project_id: int, entries: Sequence[CastIn]
) -> list[dict[str, Any]]:
    """Insert-or-patch each entry by ``(project_id, name)``.

    Returns one record per entry with ``action`` = created / updated / unchanged
    so the audit entry can say exactly what happened instead of implying a
    write happened.
    """
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        table = KIND_TABLE[entry.kind]
        name = entry.name.strip()
        key = (table, name)
        if key in seen:
            raise ValidationFailed(
                f"duplicate {entry.kind} {name!r} in one payload",
                [{"loc": "name", "msg": "appears more than once; merge before writing"}],
            )
        seen.add(key)
        fields = dict(entry.model_dump(exclude_unset=True))
        fields.pop("kind", None)
        _reject_unsupported(table, fields, entry.kind)
        for col in GENERATED_COLUMNS & set(fields):
            if fields[col]:
                raise ValidationFailed(
                    f"{col} is generated output and cannot be authored",
                    [{"loc": col, "msg": "set it in the web UI after generating"}],
                )
        existing = _find_by_name(conn, table, project_id, name)
        if existing is None:
            cols = ["project_id", *fields]
            values = [int(project_id), *fields.values()]
            cur = conn.execute(
                f"INSERT INTO {table} ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})",
                values,
            )
            results.append(
                {
                    "action": "created",
                    "id": int(cur.lastrowid),
                    "kind": entry.kind,
                    "name": name,
                }
            )
        else:
            # The action is decided by what actually changed, not by whether the
            # payload mentioned the field. Re-sending an identical payload is
            # `unchanged`, and reporting `updated` for it would put a phantom
            # edit in the audit log -- which then looks like something to undo.
            applied = _apply_patch(conn, table, int(existing["id"]), fields)
            results.append(
                {
                    "action": "updated" if applied["changed_fields"] else "unchanged",
                    "id": int(existing["id"]),
                    "kind": entry.kind,
                    "name": name,
                    **applied,
                }
            )
    return results


def _apply_patch(
    conn: sqlite3.Connection, table: str, row_id: int, fields: dict[str, Any]
) -> dict[str, Any]:
    before = conn.execute(
        f"SELECT * FROM {table} WHERE id = ?", (row_id,)
    ).fetchone()
    if before is None:  # pragma: no cover - caller just read it
        raise NotFound(f"{table} {row_id} vanished mid-write")
    effective = {k: v for k, v in fields.items() if before[k] != v}
    if effective:
        sets = ", ".join(f"{k} = ?" for k in effective)
        conn.execute(
            f"UPDATE {table} SET {sets} WHERE id = ?",
            (*effective.values(), row_id),
        )
    return {
        "changed_fields": sorted(effective),
        "before": {k: before[k] for k in effective},
    }


def update_entity(
    conn: sqlite3.Connection,
    project_id: int,
    kind: str,
    entity_id: int | str,
    patch: CastPatch,
) -> dict[str, Any]:
    """Patch one row. Resolves ``entity_id`` as either the row id or the name.

    Accepting the name matters because names are what the caller read out of
    ``cast list`` / ``project show``; forcing an id lookup first would make the
    common case two lookups and a 404 on a renamed row. See ``_resolve``.
    """
    table = KIND_TABLE[kind]
    entity_id, row = _resolve(conn, table, project_id, entity_id)
    raw = dict(patch.model_dump(exclude_unset=True))
    if "name" in raw:
        # Renaming is not offered: `name` is the upsert identity, so allowing it
        # here would create a second row and orphan the first one's references
        # in scene_characters / continuity. Report it instead of half-doing it.
        raise ValidationFailed(
            "cast update cannot rename; the web UI renames cast rows",
            [{"loc": "name", "msg": "name is the upsert identity, not an updatable field"}],
        )
    _reject_unsupported(table, raw, kind)
    fields = raw
    for col in GENERATED_COLUMNS & set(fields):
        if fields[col]:
            raise ValidationFailed(
                f"{col} is generated output and cannot be authored",
                [{"loc": col, "msg": "set it in the web UI after generating"}],
            )
    _apply_patch(conn, table, entity_id, fields)
    return get_entity(conn, project_id, kind, entity_id)
