"""CLI runtime context: connection, transaction, preflight, output.

The transaction wrapper is the load-bearing part. Every write command goes
through ``cli_txn``, which:

1. opens the one correct connection (``db.get_db`` -- WAL, busy_timeout,
   ``PRAGMA foreign_keys = ON``; opening it any other way silently disables the
   FK enforcement the whole schema depends on),
2. takes the write lock with ``BEGIN IMMEDIATE`` *before* the preflight reads,
   so nothing can slip in between "I looked" and "I wrote",
3. runs the caller's drift preflight against the rows it read,
4. runs the write, flushing one audit row in the same transaction,
5. commits, and only then writes the on-disk JSONL mirror.

Steps 4-5 are why ``cli_txn`` owns both the content callback and the audit entry.
Splitting them into "write, then log" is the exact failure the plan forbids.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from calliope.authoring import audit as audit_mod
from calliope.authoring.hash import hash_rows, scope_digest
from calliope.authoring.service import bump_revision
from calliope.cli.policy import check_op
from calliope.config import settings
from calliope.db import get_db


class CliContext:
    """Everything a command handler needs, passed as one argument."""

    def __init__(self, *, dry_run: bool = False, actor: str = "cli"):
        self.conn = get_db(settings.db_path)
        self.dry_run = dry_run
        self.actor = actor

    def close(self) -> None:
        self.conn.close()


@contextmanager
def cli_txn(
    ctx: CliContext,
    *,
    group: str,
    verb: str,
    project_id: int | None,
    args: dict[str, Any],
    write: Callable[[sqlite3.Connection, audit_mod.AuditEntry], Any],
    scopes: list[tuple[str, str, tuple]] | None = None,
) -> Iterator[dict[str, Any]]:
    """Run one write command atomically with its audit row.

    ``write`` receives the connection and the open audit entry, does its work
    through ``calliope.authoring`` helpers (which only ever take ``conn``), and
    returns the command result. ``scopes`` is the list of
    ``(table, where, params)`` to re-hash after the write for the trailing
    fingerprint.

    On any exception the transaction rolls back and nothing is written -- neither
    the data nor the log row. This is what makes "never lose data" true rather
    than aspirational.
    """
    check_op(group, verb, writes=True)
    command = f"{group} {verb}"
    conn = ctx.conn
    entry = audit_mod.audit_begin(
        conn, project_id=project_id, command=command, args=args, actor=ctx.actor
    )
    # Immediate: acquire the write lock up front so the preflight reads and the
    # writes are one indivisible window against the UI.
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = write(conn, entry)
        after_hashes = {}
        for table, where, params in scopes or ():
            rows = hash_rows(
                conn, table, project_id=project_id or 0, where=where, params=params
            )
            after_hashes[table] = scope_digest(rows)
        # A display counter only -- the guard is the content hash, not this
        # number. It answers "has anything written through the CLI touched this
        # project lately", which `log list` would otherwise need a full scan
        # for. Excluded from the hash (HASH_EXCLUDE) precisely because the CLI
        # bumps it: a counter the guard depended on would invalidate the
        # caller's own fingerprint on every write.
        if project_id is not None:
            bump_revision(conn, project_id)
        entry_id = audit_mod.audit_finish(conn, entry, after_hashes=after_hashes or None)
        if ctx.dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    if not ctx.dry_run:
        # Disk after commit, never before: if we die here SQLite already has
        # the truth and the mirror is allowed to lag.
        #
        # entry.project_id, never the cli_txn argument. `project create` opens
        # its entry with project_id=None (the row does not exist yet) and
        # re-points it at the new id inside write(), so the row and this call
        # otherwise take two different sources: the line would be filed under
        # global.jsonl with project_id null while cli_audit records the real id.
        # delete_project only ever unlinks project-{id}.jsonl, so that line
        # would outlive the project it describes.
        audit_mod.write_mirror(
            settings.data_dir,
            entry_id,
            {
                "project_id": entry.project_id,
                "command": command,
                "actor": ctx.actor,
                "changes": entry.changes,
                "summary": entry.summary(),
            },
            project_id=entry.project_id,
        )
    yield {"result": result, "entry_id": entry_id, "entry": entry, "dry_run": ctx.dry_run}


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------


def emit(payload: Any, *, as_json: bool, stream=None) -> None:
    """Print ``payload`` as JSON, or a compact human summary.

    ``--json`` is not a nicety: it is how the opencode skill reads state back
    deterministically. The text form is for humans staring at a terminal and is
    allowed to be lossy.
    """
    stream = stream or sys.stdout
    if as_json:
        json.dump(payload, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")
    else:
        stream.write(render_text(payload))
        stream.write("\n")


def render_text(payload: Any) -> str:
    if payload is None:
        return "(nothing)"
    if isinstance(payload, list):
        return "\n".join(render_text(item) for item in payload)
    if isinstance(payload, dict):
        lines = []
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                rendered = render_text(value)
                lines.append(f"{key}:\n{rendered}")
            else:
                lines.append(f"{key}: {value}")
        return "\n".join(lines)
    return str(payload)


def fail(message: str, code: int = 1, stream=None) -> int:
    stream = stream or sys.stderr
    stream.write(f"error: {message}\n")
    return code


__all__ = [
    "CliContext",
    "cli_txn",
    "emit",
    "render_text",
    "fail",
]
