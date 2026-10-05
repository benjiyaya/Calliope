"""Shared helpers for the authoring layer: id resolution and ordering.

Nothing here commits and nothing here opens a connection -- see
``authoring/__init__.py`` for the layer contract.
"""
from __future__ import annotations

import sqlite3
from typing import Any

# projects.status is free text with no CHECK, but `system` is reserved by the
# playground for its throwaway draft projects (routers/playground.py). Writing
# it from the CLI would create a project the UI hides and the user can't find.
RESERVED_STATUSES = frozenset({"system"})


class AuthoringError(Exception):
    """Base class. ``exit_code`` maps to the CLI's process exit status."""


class ValidationFailed(AuthoringError):
    """Input did not satisfy the schema. Carries a field-level report."""

    exit_code = 2

    def __init__(self, message: str, problems: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.problems = problems or []


class NotFound(AuthoringError):
    exit_code = 4


class DriftDetected(AuthoringError):
    """The rows a write expected to change had already changed under it.

    Exit code 3 by convention so a caller can tell "you must re-read" apart
    from "your input was malformed".
    """

    exit_code = 3

    def __init__(self, message: str, report: dict[str, Any] | None = None):
        super().__init__(message)
        self.report = report or {}


class PolicyError(AuthoringError):
    exit_code = 5


class OrderingError(AuthoringError):
    """order_index would break the 1..N invariant three subsystems depend on."""

    exit_code = 2


def project_or_404(conn: sqlite3.Connection, project_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM projects WHERE id = ?", (int(project_id),)
    ).fetchone()
    if row is None:
        raise NotFound(f"Project {project_id} not found")
    return row


def resolve_project(conn: sqlite3.Connection, ref: int | str) -> int:
    """Accept an id or an exact title. Titles are unique in practice but not
    by schema, so an ambiguous title is an error rather than a coin flip."""
    text = str(ref)
    if text.isdigit():
        project_or_404(conn, int(text))
        return int(text)
    rows = conn.execute(
        "SELECT id FROM projects WHERE title = ? ORDER BY id", (text,)
    ).fetchall()
    if not rows:
        raise NotFound(f"No project titled {text!r}")
    if len(rows) > 1:
        raise NotFound(
            f"Project title {text!r} is ambiguous ({len(rows)} matches); use the id"
        )
    return int(rows[0]["id"])


#: Tables the CLI is allowed to count. An f-string table name is only as safe as
#: this list -- ``cli.plan.row_counts`` interpolates the name into SQL, so the
#: allowlist is the parameterisation.
COUNTABLE_TABLES = frozenset(
    {"story_beats", "characters", "locations", "items", "scenes", "clips"}
)


def count_rows(conn: sqlite3.Connection, table: str, project_id: int) -> int:
    """``COUNT(*)`` for one project-level content table.

    The table name cannot be a bound parameter in SQLite, so it is checked
    against ``COUNTABLE_TABLES`` rather than trusted.
    """
    if table not in COUNTABLE_TABLES:
        raise ValidationFailed(
            f"{table!r} is not a project-level content table",
            [{"loc": "table", "msg": f"allowed: {sorted(COUNTABLE_TABLES)}"}],
        )
    row = conn.execute(
        f"SELECT COUNT(*) AS c FROM {table} WHERE project_id = ?", (int(project_id),)
    ).fetchone()
    return int(row["c"])


def next_order_index(conn: sqlite3.Connection, table: str, project_id: int) -> int:
    """Next order_index for a project-level sequence (1-based, dense)."""
    row = conn.execute(
        f"SELECT COALESCE(MAX(order_index), 0) AS m FROM {table} WHERE project_id = ?",
        (int(project_id),),
    ).fetchone()
    return int(row["m"]) + 1


def bump_revision(conn: sqlite3.Connection, project_id: int) -> int:
    """Increment projects.project_revision and return the new value.

    This is a display/sort marker only. It is deliberately NOT a concurrency
    guard: the web UI writes content without ever touching it, so a mismatch
    here proves nothing. The real guard is the row-hash preflight (§3.5).
    """
    conn.execute(
        "UPDATE projects SET project_revision = project_revision + 1 WHERE id = ?",
        (int(project_id),),
    )
    row = conn.execute(
        "SELECT project_revision FROM projects WHERE id = ?", (int(project_id),)
    ).fetchone()
    return int(row["project_revision"]) if row else 0


def check_status(value: str | None) -> str | None:
    if value is not None and value in RESERVED_STATUSES:
        raise ValidationFailed(
            f"status {value!r} is reserved by the playground and cannot be set "
            "through the CLI"
        )
    return value
