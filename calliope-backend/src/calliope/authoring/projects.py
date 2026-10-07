"""Project-level authoring: the row that holds the novel source text.

Note on ``idea`` (plan §4.2). The long-form source text lives in the existing
``projects.idea`` column -- the same textarea a user pastes into when creating
a project in the web UI. No column or table was added for it. That column is
read by five untruncated LLM prompt templates, which is why the built-in
generators refuse a long ``idea`` (``calliope/agent/idea_guard.py``); the CLI
path is the supported way to author such a project.

Nothing here writes a new column, changes a column type, or touches
``projects.idea``'s definition.
"""
from __future__ import annotations

import sqlite3
from typing import Any

# The one definition of "long". Imported rather than redeclared: the built-in
# generators refuse above this line and this module reports `is_long` below it,
# and two constants that drifted apart would mean the refusal and the report
# disagreeing about what counts as a novel. The layering is fine --
# `authoring.context` already imports from `agent.continuity`.
from calliope.agent.idea_guard import is_long
from calliope.authoring.service import (
    NotFound,
    ValidationFailed,
    check_status,
)
from calliope.db import row_to_dict

# How much of `idea` a *list* of projects is allowed to carry. 200 characters
# fills the two-line card clamp (`ProjectCard.svelte` uses `-webkit-line-clamp:
# 2`) with room to spare, and for the ordinary logline project it is the whole
# text, so search is unchanged for everyone not writing a novel.
IDEA_PREVIEW_CHARS = 200

# project_source is not a thing: `idea` is the source text (plan §4.2).
SOURCE_FIELDS = ("idea",)


def idea_preview(idea: str | None) -> str | None:
    """The first ``IDEA_PREVIEW_CHARS`` characters of ``idea``.

    Every project *list* -- the web UI's and the CLI's -- hands out this
    instead of the text. A list is the one response that grows with the number
    of projects times the size of their novels: four long-form projects is
    400KB+ of JSON sent to the browser on every page load, unbounded as
    projects are added. The single-project response still carries the full
    text, which is where an editor actually needs it.

    Truncated without an ellipsis on purpose. The card clamps to two lines
    anyway, so a marker would be invisible there, and in the search box it
    would be a token the user never typed.
    """
    if not idea:
        return idea
    return idea[:IDEA_PREVIEW_CHARS]


def get_project(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM projects WHERE id = ?", (int(project_id),)
    ).fetchone()
    if row is None:
        raise NotFound(f"Project {project_id} not found")
    return row_to_dict(row)


def list_projects(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    ingest_mode: str | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM projects"
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if ingest_mode:
        clauses.append("ingest_mode = ?")
        params.append(ingest_mode)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id"
    rows = [row_to_dict(r) for r in conn.execute(sql, params).fetchall()]
    # A list must not carry the novels. `project list` exists to answer "which
    # projects exist and what are they about", and a full novel per row answers
    # a question nobody asked at a cost that grows with every project added.
    for row in rows:
        row["idea_preview"] = idea_preview(row.get("idea"))
        row["idea"] = None
    return rows


def create_project(
    conn: sqlite3.Connection,
    *,
    title: str,
    idea: str | None = None,
    genre: str | None = None,
    tone: str | None = None,
    target_duration: str | None = None,
    ingest_mode: str = "builtin",
) -> int:
    if not title or not title.strip():
        raise ValidationFailed(
            "title is required", [{"loc": "title", "msg": "must not be blank"}]
        )
    if ingest_mode not in ("builtin", "external"):
        raise ValidationFailed(
            "ingest_mode must be 'builtin' or 'external'",
            [{"loc": "ingest_mode", "msg": f"got {ingest_mode!r}"}],
        )
    cur = conn.execute(
        """
        INSERT INTO projects (title, idea, genre, tone, target_duration, status,
                              ingest_mode)
        VALUES (?, ?, ?, ?, ?, 'draft', ?)
        """,
        (title.strip(), idea, genre, tone, target_duration, ingest_mode),
    )
    return int(cur.lastrowid)


def update_project(
    conn: sqlite3.Connection, project_id: int, fields: dict[str, Any]
) -> dict[str, Any]:
    if not fields:
        return get_project(conn, project_id)
    get_project(conn, project_id)  # 404 before we touch anything
    if "status" in fields:
        check_status(fields["status"])
    if "title" in fields and not str(fields["title"] or "").strip():
        raise ValidationFailed(
            "title must not be blank",
            [{"loc": "title", "msg": "must not be blank"}],
        )
    allowed = {
        "title",
        "idea",
        "genre",
        "tone",
        "target_duration",
        "status",
        "cover_path",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValidationFailed(
            f"unknown project fields: {sorted(unknown)}",
            [{"loc": k, "msg": "not a project column"} for k in sorted(unknown)],
        )
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE projects SET {sets} WHERE id = ?",
        (*fields.values(), int(project_id)),
    )
    return get_project(conn, project_id)


def idea_meta(idea: str | None) -> dict[str, Any]:
    """Describe the source text without printing it.

    ``project show`` must not dump 34k characters into an agent's context
    (plan §5.0.3). It reports presence and length only; the text itself is
    reachable through ``project source`` / ``project show --with-idea``.
    """
    text = idea or ""
    return {
        "chars": len(text),
        "is_long": is_long(text),
        "lines": text.count("\n") + 1 if text else 0,
    }


def read_source(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    from_chars: int | None = None,
    to_chars: int | None = None,
    grep: str | None = None,
    context_lines: int = 2,
) -> dict[str, Any]:
    """Read a slice of the source text, or grep it with context.

    Slicing exists so an agent can pull the 4k characters it needs instead of
    the whole novel. Offsets are character offsets, not lines, because the
    caller is usually slicing by prose position.
    """
    project = get_project(conn, project_id)
    text = project.get("idea") or ""
    meta = idea_meta(text)
    if grep:
        return {**meta, "matches": _grep(text, grep, context_lines)}
    start = max(0, int(from_chars or 0))
    end = len(text) if to_chars is None else min(len(text), int(to_chars))
    if end < start:
        raise ValidationFailed(
            "--to-chars is before --from-chars",
            [{"loc": "to_chars", "msg": f"{end} < {start}"}],
        )
    return {
        **meta,
        "from_chars": start,
        "to_chars": end,
        "truncated": end < len(text),
        "text": text[start:end],
    }


def _grep(text: str, pattern: str, context: int) -> list[dict[str, Any]]:
    lines = text.splitlines()
    low = pattern.lower()
    hits: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        if low not in line.lower():
            continue
        lo = max(0, i - context)
        hi = min(len(lines), i + context + 1)
        hits.append(
            {
                "line": i + 1,
                "text": line,
                "context_before": lines[lo:i],
                "context_after": lines[i + 1 : hi],
            }
        )
        if len(hits) >= 200:
            break
    return hits


def counts(conn: sqlite3.Connection, project_id: int) -> dict[str, int]:
    """Per-table row counts for ``project show``."""
    out: dict[str, int] = {}
    for table in (
        "story_beats",
        "characters",
        "locations",
        "items",
        "scenes",
        "clips",
    ):
        out[table] = int(
            conn.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE project_id = ?",
                (int(project_id),),
            ).fetchone()["c"]
        )
    return out
