"""Append-only audit log, written in the same transaction as the data it describes.

The contract that matters (plan §3.3): the log row and the content change commit
together or not at all. A separate log write means a crash between the two
leaves either an unexplained edit or a log entry for something that never
happened -- both are worse than no log, because the log looks trustworthy.

So this module never commits. The CLI's ``cli_txn`` owns the transaction:

    BEGIN IMMEDIATE          -- write lock taken up front, see below
    audit_begin(...)
    <content writes, each recording its before-image>
    audit_finish(...)        -- INSERT the cli_audit row
    commit
    -- only now: the JSONL mirror on disk

``BEGIN IMMEDIATE`` rather than the default deferred: a deferred transaction
takes the write lock at the first write, which is after the preflight reads.
Between those two moments the UI can commit, and our before-images are then
stale -- exactly the drift the preflight exists to catch, except we'd catch it
too late to act on it. Taking the lock first makes the preflight and the write
one atomic window.

Rollback is a NEW row carrying ``undo_of_entry_id``. Nothing is ever deleted or
mutated, so the log can always explain how the current state was reached.

Deleting a project removes its log rows via ``ON DELETE CASCADE`` inside the
same transaction as ``DELETE FROM projects``. There is no other deletion path,
and there is deliberately no ``log prune`` verb: pruning is deletion wearing a
different hat.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from calliope.authoring.service import NotFound

#: Longest ``summary`` we store. The log is read by humans scanning it; a
#: 4k-char summary is a payload dump, not a summary.
SUMMARY_CHARS = 300


class AuditEntry:
    """One in-flight change: its before-images plus the note to write.

    Created by ``audit_begin``, mutated by the content code as it touches rows,
    flushed by ``audit_finish``. Deliberately not a dataclass -- the CLI thread
    and the content helpers both hold a reference to the same one.
    """

    def __init__(
        self,
        *,
        project_id: int | None,
        command: str,
        args: dict[str, Any],
        actor: str = "cli",
    ):
        self.project_id = None if project_id is None else int(project_id)
        self.command = command
        self.args = args
        self.actor = actor
        self.changes: list[dict[str, Any]] = []
        self.summary_text = ""

    # -- content side ----------------------------------------------------

    def note(
        self,
        *,
        op: str,
        table: str,
        row_id: int | None = None,
        before: Any = None,
        after: Any = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Record one row change. ``before``/``after`` are row dicts or None."""
        change: dict[str, Any] = {"op": op, "table": table}
        if row_id is not None:
            change["id"] = int(row_id)
        if before is not None:
            change["before"] = before
        if after is not None:
            change["after"] = after
        if detail:
            change["detail"] = detail
        self.changes.append(change)

    # -- commit side -----------------------------------------------------

    def summary(self) -> str:
        """One line a human can scan the log by."""
        if self.summary_text:
            return self.summary_text
        counts: dict[str, int] = {}
        for change in self.changes:
            counts[change["op"]] = counts.get(change["op"], 0) + 1
        if not counts:
            return "no rows changed"
        return ", ".join(f"{op}×{n}" for op, n in sorted(counts.items()))


def audit_begin(
    conn: sqlite3.Connection,
    *,
    project_id: int | None,
    command: str,
    args: dict[str, Any] | None = None,
    actor: str = "cli",
) -> AuditEntry:
    return AuditEntry(
        project_id=project_id, command=command, args=args or {}, actor=actor
    )


def audit_finish(
    conn: sqlite3.Connection,
    entry: AuditEntry,
    *,
    after_hashes: dict[str, str] | None = None,
    undo_of_entry_id: int | None = None,
) -> int:
    """INSERT the ``cli_audit`` row. Does NOT commit -- the caller does that.

    Returns the new row id.
    """
    scope_hash = ""
    if after_hashes:
        joined = "|".join(f"{k}:{after_hashes[k]}" for k in sorted(after_hashes))
        scope_hash = hashlib.sha256(joined.encode()).hexdigest()[:16]
    cur = conn.execute(
        """
        INSERT INTO cli_audit (project_id, actor, command, args_json,
                               changes_json, base_scope_hash, after_scope_hash,
                               undo_of_entry_id, summary)
        VALUES (?, ?, ?, ?, ?, '', ?, ?, ?)
        """,
        (
            entry.project_id,
            entry.actor,
            entry.command,
            json.dumps(entry.args, ensure_ascii=False, default=str),
            json.dumps(entry.changes, ensure_ascii=False, default=str),
            scope_hash,
            undo_of_entry_id,
            entry.summary()[:SUMMARY_CHARS],
        ),
    )
    return int(cur.lastrowid)


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


def list_entries(
    conn: sqlite3.Connection,
    *,
    project_id: int | None = None,
    command: str | None = None,
    limit: int = 50,
    since_id: int | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM cli_audit WHERE 1=1"
    params: list[Any] = []
    if project_id is not None:
        sql += " AND project_id = ?"
        params.append(int(project_id))
    if command:
        sql += " AND command = ?"
        params.append(command)
    if since_id is not None:
        sql += " AND id > ?"
        params.append(int(since_id))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    rows = []
    for row in conn.execute(sql, params).fetchall():
        item = {k: row[k] for k in row.keys()}
        for key in ("args_json", "changes_json"):
            try:
                item[key[: -len("_json")]] = json.loads(item.pop(key))
            except json.JSONDecodeError:  # pragma: no cover - written by us
                item[key[: -len("_json")]] = None
        rows.append(item)
    return rows


def get_entry(conn: sqlite3.Connection, entry_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM cli_audit WHERE id = ?", (int(entry_id),)
    ).fetchone()
    if row is None:
        raise NotFound(f"Audit entry {entry_id} not found")
    item = {k: row[k] for k in row.keys()}
    for key in ("args_json", "changes_json"):
        try:
            item[key[: -len("_json")]] = json.loads(item.pop(key))
        except json.JSONDecodeError:  # pragma: no cover
            item[key[: -len("_json")]] = None
    return item


# --------------------------------------------------------------------------
# on-disk mirror
# --------------------------------------------------------------------------

#: Mirror files live here. The only two directories the CLI may write to
#: (plan §3.6); everything else is off limits.
AUDIT_DIRNAME = "audit"


def mirror_path(data_dir: Path, project_id: int | None) -> Path:
    name = "global.jsonl" if project_id is None else f"project-{int(project_id)}.jsonl"
    return Path(data_dir) / AUDIT_DIRNAME / name


def write_mirror(
    data_dir: Path, entry_id: int, record: dict[str, Any], *, project_id: int | None
) -> Path:
    """Append one committed entry to its JSONL mirror.

    Called only AFTER the SQLite commit. The mirror is a convenience for
    grepping and offline inspection; if the process dies here the authoritative
    log in SQLite is already complete, so the mirror is allowed to lag.
    """
    path = mirror_path(data_dir, project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"entry_id": entry_id, **record}, ensure_ascii=False, default=str
    )
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")
    return path


def project_log_files(data_dir: Path, project_id: int) -> list[Path]:
    """Files to remove when a project is deleted.

    Returned as a list and deleted by the caller only AFTER the commit that
    deleted the project: touching disk inside the transaction would leave a file
    whose project no longer exists if the rollback happened, and a log entry
    pointing at rows that came back if it did not.
    """
    audit_dir = Path(data_dir) / AUDIT_DIRNAME
    files: list[Path] = []
    for candidate in (
        audit_dir / f"project-{int(project_id)}.jsonl",
        Path(data_dir) / "cli_snapshots" / f"project-{int(project_id)}",
    ):
        if candidate.exists():
            files.append(candidate)
    return files


def remove_files(paths: list[Path]) -> list[str]:
    """Delete ``paths`` (files or directories). Returns what was removed."""
    removed: list[str] = []
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(str(path))
        elif path.exists():
            path.unlink()
            removed.append(str(path))
    return removed


def is_inside(candidate: Path, root: Path) -> bool:
    """Containment check for any path the CLI is asked to touch."""
    try:
        Path(os.path.normpath(candidate)).relative_to(Path(os.path.normpath(root)))
    except ValueError:
        return False
    return True