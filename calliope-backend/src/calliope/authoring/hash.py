"""Row and scope hashing: the basis of the write-time preflight.

Why this exists (plan §3.5). The v1 design used a ``cli_revision`` counter as
a concurrency guard. That was a real bug: the web UI writes content without
ever incrementing it, so the counter proved nothing. Worse, the eight content
tables the CLI touches have no ``updated_at`` column at all, so timestamps
cannot cover the gap either.

So the guard is the *content itself*. Before a write, hash every row the write
intends to touch and compare against the hash recorded when the caller read
them. Any difference -- including a change the UI made behind the CLI's back --
shows up as drift. No column, no counter, nothing to keep in sync.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from calliope.authoring.service import ValidationFailed

# Columns excluded from the hash on purpose:
#   created_at / updated_at -- bookkeeping; touching them is not a content change
#   project_revision       -- CLI bookkeeping, bumped by our own writes
# Columns deliberately INCLUDED despite living on projects:
#   idea                   -- the novel source text lives here (plan §4.2), so a
#                             user editing the source must invalidate a pending
#                             write of beats
HASH_EXCLUDE: dict[str, frozenset[str]] = {
    "projects": frozenset({"created_at", "updated_at", "project_revision"}),
    "story_beats": frozenset({"created_at"}),
    "characters": frozenset({"created_at"}),
    "locations": frozenset({"created_at"}),
    "items": frozenset({"created_at"}),
    "scenes": frozenset({"created_at", "updated_at"}),
    "clips": frozenset({"created_at", "updated_at"}),
    "agent_memory": frozenset({"created_at", "last_used_at", "use_count"}),
}

#: Every table this module will hash, and the column that scopes it to a
#: project. ``projects`` is the odd one out: it IS the project, so it is keyed
#: by ``id`` and has no ``project_id`` column at all.
#:
#: This doubles as the allowlist that makes the f-string in ``hash_rows``
#: safe -- the table name cannot be a bound parameter, so it is checked rather
#: than trusted.
SCOPE_KEY: dict[str, str] = {
    "projects": "id",
    "story_beats": "project_id",
    "characters": "project_id",
    "locations": "project_id",
    "items": "project_id",
    "scenes": "project_id",
    "clips": "project_id",
    "agent_memory": "project_id",
}

#: The subset a CLI write can guard. ``agent_memory`` is hashed because the
#: board needs it, but no write verb preflights it, so ``project hash`` does not
#: advertise one -- reporting a value nothing consumes teaches the caller that
#: every number in the output is a usable ``--expect-hash``.
WRITABLE_SCOPES = frozenset(SCOPE_KEY) - {"agent_memory"}


def canonical(value: Any) -> Any:
    """Normalize a row value so hashing is stable across reads.

    SQLite hands back TEXT for timestamps and ints for order columns, and a
    column added by a later migration must not silently change every hash.
    """
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, (int, str, bool)) or value is None:
        return value
    return str(value)


def row_hash(table: str, row: sqlite3.Row | dict[str, Any]) -> str:
    """Stable hash of one row's content, ignoring bookkeeping columns."""
    data = {k: row[k] for k in row.keys()} if isinstance(row, sqlite3.Row) else dict(row)
    skip = HASH_EXCLUDE.get(table, frozenset())
    payload = {k: canonical(v) for k, v in sorted(data.items()) if k not in skip}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def rows_hash(table: str, rows: Sequence[sqlite3.Row | dict[str, Any]]) -> str:
    """Hash an ordered set of rows. Order is part of the identity: reordering
    beats is a real change the caller must see."""
    parts = [row_hash(table, r) for r in rows]
    blob = "|".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def scope_hash(hashes: Iterable[str]) -> str:
    """Combine per-row hashes into one scope fingerprint."""
    joined = "|".join(hashes)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def scope_digest(rows: Mapping[int, str], *, with_ids: bool = False) -> str:
    """One fingerprint for a ``{rowid: row_hash}`` map, in id order.

    Sorted by id on purpose. ``hash_rows`` happens to build the map in id
    order, but a caller that merged two maps or rebuilt one from a dict
    comprehension over a query without ``ORDER BY`` would silently produce a
    different digest for identical content -- and then every write would report
    drift that is not there.

    ``with_ids`` is the same value with the row ids folded in. Both forms are
    accepted by ``--expect-hash``; the id-carrying one is what a human can read
    back and reconcile against ``log show``.
    """
    joined = "|".join(
        f"{i}={rows[i]}" if with_ids else rows[i] for i in sorted(rows)
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def project_digests(conn: sqlite3.Connection, project_id: int) -> dict[str, str]:
    """``{table: scope digest}`` for every table a CLI write guards.

    This is how a caller obtains the value for ``--expect-hash``. Without it the
    option is unusable in practice: the guard is the whole point of the
    read-then-write discipline, and a fingerprint nobody can obtain is a
    fingerprint nobody supplies.
    """
    return {
        table: scope_digest(hash_rows(conn, table, project_id=project_id))
        for table in sorted(SCOPE_KEY)
        if table in WRITABLE_SCOPES
    }


def hash_rows(
    conn: sqlite3.Connection,
    table: str,
    *,
    project_id: int,
    where: str = "",
    params: Sequence[Any] = (),
) -> dict[int, str]:
    """Map ``rowid -> row_hash`` for the rows a write may touch.

    ``where`` is caller-supplied SQL. It exists because every scope in the
    design is a range or a subset (``order_index BETWEEN ? AND ?``), not the
    whole table. Callers inside this package pass literals only -- there is no
    user string anywhere in the path.
    """
    key = SCOPE_KEY.get(table)
    if key is None:
        raise ValidationFailed(
            f"{table!r} is not a hashable scope table",
            [{"loc": "table", "msg": f"allowed: {sorted(SCOPE_KEY)}"}],
        )
    sql = f"SELECT * FROM {table} WHERE {key} = ?"
    if where:
        sql += f" AND ({where})"
    sql += " ORDER BY id"
    return {
        int(r["id"]): row_hash(table, r)
        for r in conn.execute(sql, (int(project_id), *params)).fetchall()
    }
