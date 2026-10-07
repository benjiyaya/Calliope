"""``plan next``: one pure function deciding what to write next.

Extracted deliberately. The rule -- beats are the spine, cast before script (a
scene names characters), script before clips, clips before continuity (the
ledger describes shots that must exist) -- is the single most repeated piece of
advice in the opencode skill, and advice that lives only in prose drifts the
moment someone adds a group. One function, one place, unit-tested without a
model: ``tests/test_cli_plan.py`` walks every branch.

Two things this deliberately does not do:

* It never calls the model. ``ensure_continuity_plan`` would, and it is not
  importable from here -- refresh stays a UI action. What this reports is
  staleness, which is a comparison, not a generation.
* It does not know whether the source text is loaded. A project whose novel is
  in ``projects.idea`` and one that was ingested externally are in the same
  state at this level; ``projects.ingest_mode`` records which, but neither
  changes what to write next.

No SQL here. Every count comes from ``authoring``.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from calliope.authoring import beats as beats_mod
from calliope.authoring import context as ctx_mod
from calliope.authoring import script as script_mod
from calliope.authoring.service import count_rows

#: Tables whose row counts decide the ordering, in the order they gate.
GATING_TABLES = ("story_beats", "characters", "scenes", "clips")


def next_step(conn: sqlite3.Connection, project_id: int) -> dict[str, Any]:
    """What the caller should write next, and why.

    Returns a ``step`` code the skill branches on, plus the counts that produced
    the decision, so the caller can see the reasoning instead of trusting it.
    """
    # Raises NotFound for an unknown project -- the same error every other read
    # raises, which is what makes `plan next` exit 4 like the rest.
    board_counts = row_counts(conn, project_id)
    state = ctx_mod.plan_state(conn, project_id)
    unshot = script_mod.undescribed_scenes(conn, project_id)
    # Reported, never acted on. `scenes`/`clips` are renumbered dense on write
    # and a violation raises; beats are not, because the web UI is allowed to
    # leave gaps. So a gap survives silently unless something looks -- and
    # `plan next` is the one command a caller is guaranteed to run, which makes
    # it the cheapest place to say so.
    beat_order = beats_mod.order_report(conn, project_id)

    # Ordered first-match. The order IS the contract. `beat_order` rides along on
    # every branch purely to populate `note`; it never gates.
    if not board_counts["story_beats"]:
        return _step(
            "story.append", project_id, board_counts, state,
            "no beats yet; write the beat list from the source text first",
            hint="calliope-cli schema show story append",
            beat_order=beat_order,
        )
    if not board_counts["characters"]:
        return _step(
            "cast.upsert", project_id, board_counts, state,
            "beats exist but no cast; scenes reference characters by name",
            hint="calliope-cli schema show cast upsert",
            beat_order=beat_order,
        )
    if not board_counts["scenes"]:
        return _step(
            "script.append", project_id, board_counts, state,
            "cast exists but the script is empty; write scenes",
            hint="calliope-cli schema show script append",
            beat_order=beat_order,
        )
    if unshot:
        return _step(
            "clips.append", project_id, board_counts, state,
            f"{len(unshot)} scene(s) still have only the default clip; "
            "expand them into shots",
            targets=[int(s["id"]) for s in unshot][:20],
            hint="calliope-cli schema show clips append",
            beat_order=beat_order,
        )
    # Gated on `authored`, not on `stale`. `stale` tracks the derived `shots`,
    # which only `ensure_continuity_plan` can refresh -- it needs the model, and
    # the CLI never calls the model. Gating on it would report `context.set`
    # after every ledger write and the caller would rewrite it forever.
    if not state["authored"]:
        return _step(
            "context.set", project_id, board_counts, state,
            "every clip has a description but the ledger has no authored "
            "content yet; write overview + requirements",
            hint="calliope-cli schema show context set",
            beat_order=beat_order,
        )
    return _step(
        "render", project_id, board_counts, state,
        "script, shots, and ledger are all written; render in the web UI "
        "(the CLI never calls the model)",
        beat_order=beat_order,
        # Surfaced, not acted on: the per-shot plan is derived and the UI
        # recalculates it on render. Worth saying out loud so the caller does
        # not read `stale: true` as unfinished CLI work.
        note=(
            f"the derived shot plan is stale (stored based_on "
            f"{state['stored_based_on'] or '(none)'} != {state['basis_hash']}); "
            "the web UI refreshes it on render -- no CLI action"
        )
        if state["stale"]
        else None,
    )


def row_counts(conn: sqlite3.Connection, project_id: int) -> dict[str, int]:
    """Row count per gating table."""
    counts: dict[str, int] = {}
    for table in GATING_TABLES:
        counts[table] = count_rows(conn, table, project_id)
    return counts


def _step(
    step: str,
    project_id: int,
    counts: dict[str, int],
    state: dict[str, Any],
    why: str,
    *,
    targets: list[int] | None = None,
    hint: str | None = None,
    note: str | None = None,
    beat_order: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "project_id": int(project_id),
        "step": step,
        "why": why,
        "counts": counts,
        "plan_state": state["state"],
        "basis_hash": state["basis_hash"],
    }
    if targets:
        out["targets"] = targets
    if hint:
        out["hint"] = hint
    notes = [n for n in (note, _beat_order_note(beat_order)) if n]
    if notes:
        out["note"] = " | ".join(notes)
    return out


def _beat_order_note(beat_order: dict[str, Any] | None) -> str | None:
    """One sentence about a gapped or duplicated beat sequence, or ``None``.

    Advisory only -- it never changes ``step``. A caller in the middle of
    writing beats should not be told to stop; but a caller about to
    ``replace-range`` by index needs to know the indices do not mean what they
    look like, and the cheapest moment to learn that is before they read.
    """
    if not beat_order or beat_order["dense"]:
        return None
    parts = []
    if beat_order["duplicates"]:
        dupes = ", ".join(
            f"#{d['order_index']} (ids {', '.join(str(i) for i in d['ids'])})"
            for d in beat_order["duplicates"][:5]
        )
        parts.append(f"duplicate order_index at {dupes}")
    if beat_order["holes"]:
        holes = ", ".join(str(h) for h in beat_order["holes"][:10])
        more = "..." if len(beat_order["holes"]) > 10 else ""
        parts.append(f"holes at {holes}{more}")
    if not parts:
        return None
    return (
        "story_beats.order_index is not a dense 1..N sequence ("
        + "; ".join(parts)
        + ") -- a soft invariant the web UI may also leave; check `story order` "
        "before replacing a range by index"
    )
