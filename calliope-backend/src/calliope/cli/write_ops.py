"""Write commands. Every one of them takes the same shape.

    def op_x(ctx, args):
        pid = resolve_project(ctx.conn, args.project)
        payload = validate_payload("story", "append", load_payload(args.file))
        with cli_txn(ctx, group="story", verb="append", project_id=pid,
                     args=..., write=lambda conn, entry: _do(conn, entry, pid, payload),
                     scopes=[("story_beats", "", ())]) as txn:
            return txn

The invariant that makes this safe is that ``_do`` never commits and never opens
a connection. ``cli_txn`` owns the transaction so the content write and the
audit row are one atomic unit.

Drift preflight: any command that overwrites or replaces existing rows takes a
``--expect-hash`` (the fingerprint the caller last read) and refuses to write
if the rows moved. Commands that only append have nothing to preflight -- there
is no earlier state to have drifted from.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Callable

from calliope.authoring import beats as beats_mod
from calliope.authoring import cast as cast_mod
from calliope.authoring import context as ctx_mod
from calliope.authoring import projects as proj_mod
from calliope.authoring import script as script_mod
from calliope.authoring.audit import AuditEntry
from calliope.authoring.hash import hash_rows, scope_digest
from calliope.authoring.models import validate_payload
from calliope.authoring.service import DriftDetected, ValidationFailed, resolve_project
from calliope.cli.context import CliContext, cli_txn
from calliope.cli.io import load_payload


def _pid(ctx: CliContext, args: Any) -> int:
    return resolve_project(ctx.conn, args.project)


def _guard(
    conn: sqlite3.Connection,
    *,
    table: str,
    project_id: int,
    expected: str | None,
) -> None:
    """Compare a caller-supplied scope hash against the live rows.

    Runs inside the write transaction, so the comparison and the write read the
    same snapshot and cannot be separated by a concurrent UI edit.

    Both forms are accepted: the bare digest from ``project hash``, and the
    id-carrying digest from ``project hash --with-ids`` (easier to reconcile by
    hand against ``log show``).

    Raises ``DriftDetected`` (exit 3), not ``ValidationFailed`` (exit 2). The
    caller's payload is fine -- the *world* moved. A caller scripting a retry
    has to be able to tell those apart: one means "fix your JSON", the other
    means "re-read and re-apply on top of what is there now".
    """
    if not expected:
        return
    now = hash_rows(conn, table, project_id=project_id)
    digest = scope_digest(now)
    if expected not in (digest, scope_digest(now, with_ids=True)):
        raise DriftDetected(
            f"{table} changed since you read it (you saw {expected}, it is now "
            f"{digest}). Re-read with `project hash` plus this table's read "
            f"command, then re-apply your change on top of what is there now. "
            f"`log list --project {project_id}` shows what the CLI last wrote.",
            {"table": table, "expected": expected, "now": digest},
        )


# -- project ---------------------------------------------------------------


def _project_create(ctx: CliContext, args: Any) -> Any:
    def write(conn: sqlite3.Connection, entry: AuditEntry) -> Any:
        pid = proj_mod.create_project(
            conn,
            title=args.title,
            idea=args.idea,
            genre=args.genre,
            tone=args.tone,
            target_duration=args.target_duration,
            ingest_mode=args.ingest_mode,
        )
        entry.project_id = pid
        entry.note(op="insert", table="projects", row_id=pid,
                   after={"title": args.title})
        return {"project_id": pid, "title": args.title}

    with cli_txn(
        ctx, group="project", verb="create", project_id=None,
        args={"title": args.title, "ingest_mode": args.ingest_mode},
        write=write,
    ) as txn:
        return txn["result"]


def _project_set(ctx: CliContext, args: Any) -> Any:
    pid = _pid(ctx, args)
    fields = {
        k: v
        for k, v in args.patch.model_dump(exclude_unset=True).items()
        if v is not None
    }

    def write(conn: sqlite3.Connection, entry: AuditEntry) -> Any:
        _guard(conn, table="projects", project_id=pid, expected=args.expect_hash)
        before = proj_mod.get_project(conn, pid)
        after = proj_mod.update_project(conn, pid, fields)
        entry.note(op="update", table="projects", row_id=pid,
                   before=before, after=after)
        return {"project_id": pid, **{k: v for k, v in after.items() if k in fields},
                "source": proj_mod.idea_meta(after.get("idea"))}

    with cli_txn(
        ctx, group="project", verb="set", project_id=pid,
        args={"project": args.project, "fields": sorted(fields)},
        write=write, scopes=[("projects", "", ())],
    ) as txn:
        return txn["result"]


# -- story -----------------------------------------------------------------


def _story_append(ctx: CliContext, args: Any) -> Any:
    pid = _pid(ctx, args)
    beats = validate_payload("story", "append", load_payload(args.file))

    def write(conn: sqlite3.Connection, entry: AuditEntry) -> Any:
        # Guarded like every other write that carries an explicit scope: the
        # caller read order_index before choosing it, so a row the web UI landed
        # in between would be over-written onto a position that is now taken.
        _guard(conn, table="story_beats", project_id=pid, expected=args.expect_hash)
        ids = beats_mod.append_beats(conn, pid, beats)
        for beat_id, beat in zip(ids, beats):
            entry.note(op="insert", table="story_beats", row_id=beat_id,
                       after={"title": beat.title, "order_index": beat.order_index})
        return {"project_id": pid, "beat_ids": ids}

    with cli_txn(
        ctx, group="story", verb="append", project_id=pid,
        args={"project": args.project, "count": len(beats)},
        write=write, scopes=[("story_beats", "", ())],
    ) as txn:
        return txn["result"]


def _story_update(ctx: CliContext, args: Any) -> Any:
    pid = _pid(ctx, args)

    def write(conn: sqlite3.Connection, entry: AuditEntry) -> Any:
        _guard(conn, table="story_beats", project_id=pid, expected=args.expect_hash)
        before = beats_mod.get_beat(conn, pid, args.beat_id)
        after = beats_mod.update_beat(conn, pid, args.beat_id, args.patch)
        entry.note(op="update", table="story_beats", row_id=args.beat_id,
                   before=before, after=after)
        return {"project_id": pid, "beat_id": args.beat_id,
                "changed": _changed(before, after)}

    with cli_txn(
        ctx, group="story", verb="update", project_id=pid,
        args={"project": args.project, "beat_id": args.beat_id},
        write=write, scopes=[("story_beats", "", ())],
    ) as txn:
        return txn["result"]


def _story_replace(ctx: CliContext, args: Any) -> Any:
    pid = _pid(ctx, args)
    beats = validate_payload("story", "replace-range", load_payload(args.file))

    def write(conn: sqlite3.Connection, entry: AuditEntry) -> Any:
        _guard(conn, table="story_beats", project_id=pid, expected=args.expect_hash)
        result = beats_mod.replace_range(
            conn, pid, from_index=args.from_index, to_index=args.to_index, beats=beats
        )
        entry.note(op="replace", table="story_beats",
                   detail={"range": result["range"],
                           "removed": [_brief(b) for b in result["removed"]],
                           "added_ids": result["added_ids"]})
        return {"project_id": pid, **result, "removed": [_brief(b) for b in result["removed"]]}

    with cli_txn(
        ctx, group="story", verb="replace-range", project_id=pid,
        args={"project": args.project, "range": [args.from_index, args.to_index],
              "count": len(beats)},
        write=write, scopes=[("story_beats", "", ())],
    ) as txn:
        return txn["result"]


# -- cast ------------------------------------------------------------------


def _cast_upsert(ctx: CliContext, args: Any) -> Any:
    pid = _pid(ctx, args)
    entries = validate_payload("cast", "upsert", load_payload(args.file))
    if args.kind:
        # help: "override the kind of every row in the payload". The flag was
        # parsed and never read, so --kind location wrote characters and
        # reported success -- a wrong-table write, the one failure mode that
        # looks like nothing went wrong.
        for row in entries:
            row.kind = args.kind
    # One --expect-hash value names one scope. A payload that stays inside a
    # single table is guarded exactly like every other write; one that spans
    # tables has no fingerprint that could match them all, so the combination is
    # refused instead of checking one table and leaving the rest to look
    # covered. With no value supplied nothing here changes -- the flag is
    # optional and stays optional.
    tables = sorted({cast_mod.KIND_TABLE[row.kind] for row in entries})
    if args.expect_hash and len(tables) > 1:
        raise ValidationFailed(
            f"cast upsert spans {len(tables)} tables ({', '.join(tables)}); "
            "--expect-hash compares a single scope and cannot cover more than "
            "one of them. Split the payload so each write stays inside one "
            "table, or drop --expect-hash and compare `project hash` per table "
            "yourself before writing.",
            [{"loc": "expect-hash", "msg": f"tables: {tables}"}],
        )

    def write(conn: sqlite3.Connection, entry: AuditEntry) -> Any:
        if args.expect_hash and len(tables) == 1:
            _guard(conn, table=tables[0], project_id=pid,
                   expected=args.expect_hash)
        results = cast_mod.upsert(conn, pid, entries)
        for record in results:
            table = cast_mod.KIND_TABLE[record["kind"]]
            entry.note(op="insert" if record["action"] == "created" else "update",
                       table=table, row_id=record["id"],
                       detail=record)
        return {"project_id": pid, "results": results}

    with cli_txn(
        ctx, group="cast", verb="upsert", project_id=pid,
        args={"project": args.project, "count": len(entries)},
        write=write, scopes=[("characters", "", ()), ("locations", "", ()),
                             ("items", "", ())],
    ) as txn:
        return txn["result"]


def _cast_update(ctx: CliContext, args: Any) -> Any:
    pid = _pid(ctx, args)

    def write(conn: sqlite3.Connection, entry: AuditEntry) -> Any:
        table = cast_mod.KIND_TABLE[args.kind]
        _guard(conn, table=table, project_id=pid, expected=args.expect_hash)
        before = cast_mod.get_entity(conn, pid, args.kind, args.entity_id)
        after = cast_mod.update_entity(
            conn, pid, args.kind, args.entity_id, args.patch
        )
        entry.note(op="update", table=table, row_id=after["id"],
                   before=before, after=after)
        return {"project_id": pid, "kind": args.kind, "id": after["id"],
                "changed": _changed(before, after)}

    with cli_txn(
        ctx, group="cast", verb="update", project_id=pid,
        args={"project": args.project, "kind": args.kind, "entity_id": args.entity_id},
        write=write, scopes=[(cast_mod.KIND_TABLE[args.kind], "", ())],
    ) as txn:
        return txn["result"]


# -- script ----------------------------------------------------------------


def _script_append(ctx: CliContext, args: Any) -> Any:
    pid = _pid(ctx, args)
    scenes = validate_payload("script", "append", load_payload(args.file))

    def write(conn: sqlite3.Connection, entry: AuditEntry) -> Any:
        _guard(conn, table="scenes", project_id=pid, expected=args.expect_hash)
        ids = script_mod.append_scenes(conn, pid, scenes)
        script_mod.assert_invariants(conn, pid)
        for scene_id, scene in zip(ids, scenes):
            entry.note(op="insert", table="scenes", row_id=scene_id,
                       after={"heading": scene.heading, "action": scene.action})
        return {"project_id": pid, "scene_ids": ids}

    with cli_txn(
        ctx, group="script", verb="append", project_id=pid,
        args={"project": args.project, "count": len(scenes)},
        write=write, scopes=[("scenes", "", ())],
    ) as txn:
        return txn["result"]


def _script_update(ctx: CliContext, args: Any) -> Any:
    pid = _pid(ctx, args)

    def write(conn: sqlite3.Connection, entry: AuditEntry) -> Any:
        _guard(conn, table="scenes", project_id=pid, expected=args.expect_hash)
        before = script_mod.get_scene(conn, pid, args.scene_id)
        after = script_mod.update_scene(conn, pid, args.scene_id, args.patch)
        script_mod.assert_invariants(conn, pid)
        entry.note(op="update", table="scenes", row_id=args.scene_id,
                   before=_brief(before), after=_brief(after))
        return {"project_id": pid, "scene_id": args.scene_id,
                "changed": _changed(before, after)}

    with cli_txn(
        ctx, group="script", verb="update", project_id=pid,
        args={"project": args.project, "scene_id": args.scene_id},
        write=write, scopes=[("scenes", "", ())],
    ) as txn:
        return txn["result"]


def _script_replace(ctx: CliContext, args: Any) -> Any:
    pid = _pid(ctx, args)
    scenes = validate_payload("script", "replace-range", load_payload(args.file))

    def write(conn: sqlite3.Connection, entry: AuditEntry) -> Any:
        _guard(conn, table="scenes", project_id=pid, expected=args.expect_hash)
        result = script_mod.replace_scene_range(
            conn, pid, from_index=args.from_index, to_index=args.to_index, scenes=scenes
        )
        script_mod.assert_invariants(conn, pid)
        entry.note(op="replace", table="scenes",
                   detail={"range": result["range"],
                           "removed": [_brief(s) for s in result["removed"]],
                           "removed_clips": [_brief(c) for c in result["removed_clips"]],
                           "removed_characters": result["removed_characters"],
                           "added_ids": result["added_ids"]})
        return {"project_id": pid,
                "range": result["range"],
                "removed": [_brief(s) for s in result["removed"]],
                "removed_clips": [_brief(c) for c in result["removed_clips"]],
                "added_ids": result["added_ids"]}

    with cli_txn(
        ctx, group="script", verb="replace-range", project_id=pid,
        args={"project": args.project, "range": [args.from_index, args.to_index],
              "count": len(scenes)},
        write=write, scopes=[("scenes", "", ())],
    ) as txn:
        return txn["result"]


# -- clips -----------------------------------------------------------------


def _clips_append(ctx: CliContext, args: Any) -> Any:
    pid = _pid(ctx, args)
    clips = validate_payload("clips", "append", load_payload(args.file))
    scene_id = _scene_arg(ctx, args, pid)

    def write(conn: sqlite3.Connection, entry: AuditEntry) -> Any:
        _guard(conn, table="clips", project_id=pid, expected=args.expect_hash)
        ids = script_mod.append_clips(conn, pid, scene_id, clips)
        script_mod.assert_invariants(conn, pid)
        for clip_id, clip in zip(ids, clips):
            entry.note(op="insert", table="clips", row_id=clip_id,
                       after={"scene_id": scene_id, "description": clip.description,
                             "shot_size": clip.shot_size})
        return {"project_id": pid, "scene_id": scene_id, "clip_ids": ids}

    with cli_txn(
        ctx, group="clips", verb="append", project_id=pid,
        args={"project": args.project, "scene_id": scene_id, "count": len(clips)},
        write=write, scopes=[("clips", "", ())],
    ) as txn:
        return txn["result"]


def _clips_update(ctx: CliContext, args: Any) -> Any:
    pid = _pid(ctx, args)

    def write(conn: sqlite3.Connection, entry: AuditEntry) -> Any:
        _guard(conn, table="clips", project_id=pid, expected=args.expect_hash)
        before = script_mod.get_clip(conn, pid, args.clip_id)
        after = script_mod.update_clip(conn, pid, args.clip_id, args.patch)
        script_mod.assert_invariants(conn, pid)
        entry.note(op="update", table="clips", row_id=args.clip_id,
                   before=before, after=after)
        return {"project_id": pid, "clip_id": args.clip_id,
                "changed": _changed(before, after)}

    with cli_txn(
        ctx, group="clips", verb="update", project_id=pid,
        args={"project": args.project, "clip_id": args.clip_id},
        write=write, scopes=[("clips", "", ())],
    ) as txn:
        return txn["result"]


def _clips_replace(ctx: CliContext, args: Any) -> Any:
    pid = _pid(ctx, args)
    clips = validate_payload("clips", "replace-range", load_payload(args.file))
    scene_id = _scene_arg(ctx, args, pid)

    def write(conn: sqlite3.Connection, entry: AuditEntry) -> Any:
        _guard(conn, table="clips", project_id=pid, expected=args.expect_hash)
        result = script_mod.replace_clip_range(
            conn, pid, scene_id,
            from_index=args.from_index, to_index=args.to_index, clips=clips,
        )
        script_mod.assert_invariants(conn, pid)
        entry.note(op="replace", table="clips",
                   detail={"scene_id": scene_id, "range": result["range"],
                           "removed": [_brief(c) for c in result["removed"]],
                           "added_ids": result["added_ids"]})
        return {"project_id": pid,
                "range": result["range"],
                "removed": [_brief(c) for c in result["removed"]],
                "added_ids": result["added_ids"]}

    with cli_txn(
        ctx, group="clips", verb="replace-range", project_id=pid,
        args={"project": args.project, "scene_id": scene_id,
              "range": [args.from_index, args.to_index], "count": len(clips)},
        write=write, scopes=[("clips", "", ())],
    ) as txn:
        return txn["result"]


def _scene_arg(ctx: CliContext, args: Any, pid: int) -> int:
    """Resolve ``--scene``: an id, or a 1-based scene position.

    Accepting the position matters because the caller just read the script as an
    ordered list -- making it translate position to row id by hand is the kind of
    off-by-one that silently retitles the wrong scene.
    """
    value = args.scene
    if value is None:
        raise ValidationFailed("--scene is required", [{"loc": "scene", "msg": "missing"}])
    if str(value).isdigit() and int(value) >= 1000:
        return int(value)
    rows = script_mod.list_scenes(ctx.conn, pid)
    for row in rows:
        if int(row["order_index"]) == int(value):
            return int(row["id"])
    # A small number that is not a position may still be a row id.
    for row in rows:
        if int(row["id"]) == int(value):
            return int(row["id"])
    raise ValidationFailed(
        f"no scene #{value} in project {pid}",
        [{"loc": "scene", "msg": "expected a scene position (1-based) or a scene id"}],
    )


# -- context ---------------------------------------------------------------


def _context_set(ctx: CliContext, args: Any) -> Any:
    pid = _pid(ctx, args)
    payload = load_payload(args.file)
    # The guard compares the whole projects row, idea included. There is no
    # narrower scope to compare, because continuity_json and the source text
    # share one row and cannot be fingerprinted apart.
    #
    # An earlier note here declined the guard on cost grounds -- "hashing 34k
    # characters to set four short strings". Measured on the real database that
    # is 0.13 ms for an 8,581-character novel (0.093 ms is the SHA256 of 34,000
    # characters alone), against 0.20 ms for the whole `project hash` the caller
    # has already run to obtain this value. The cost was never the reason to
    # leave the help text promising a comparison that did not happen.
    #
    # What the guard does cost is precision: retyping the novel in the web UI
    # makes a pending context write re-read, even though continuity_json itself
    # did not move. That errs the safe way -- refuse rather than overwrite.
    if payload is None:
        raise ValidationFailed(
            "context set needs --file", [{"loc": "file", "msg": "missing"}]
        )
    # One document, not a row batch: validate_payload unwraps the
    # single-element array form and rejects anything longer, so there is never a
    # question of which element wins.
    model = validate_payload("context", "set", payload)[0]

    def write(conn: sqlite3.Connection, entry: AuditEntry) -> Any:
        _guard(conn, table="projects", project_id=pid, expected=args.expect_hash)
        before = ctx_mod.load_stored_plan(conn, pid)
        after = ctx_mod.set_context(
            conn, pid, overview=model.overview, requirements=model.requirements
        )
        entry.note(op="update", table="projects", row_id=pid,
                   before={"continuity_json": before}, after={"continuity_json": after})
        return {"project_id": pid, "overview": after["overview"],
                "requirements": after["requirements"],
                "shots_preserved": len(after["shots"]),
                "note": "per-clip constraints belong in clips.description as [CARRY] lines"}

    with cli_txn(
        ctx, group="context", verb="set", project_id=pid,
        args={"project": args.project, "overview": sorted(model.overview),
              "requirements": sorted(model.requirements)},
        write=write, scopes=[("projects", "", ())],
    ) as txn:
        return txn["result"]


# -- helpers ---------------------------------------------------------------


def _brief(row: dict[str, Any]) -> dict[str, Any]:
    """Trim a removed row for the audit entry.

    Full before-images would bloat the log with every clip's dialog and every
    scene's action; the audit needs to identify the row and let ``undo`` fetch
    the rest from the recorded hash. Long text is truncated, not dropped.
    """
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, str) and len(value) > 200:
            out[key] = value[:200] + f"...[{len(value)} chars]"
        else:
            out[key] = value
    return out


def _changed(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {
        k: {"from": before.get(k), "to": after.get(k)}
        for k in after
        if before.get(k) != after.get(k)
    }


#: (group, verb, handler, scope-tables). ``scope-tables`` are re-hashed after
#: the write so the audit row carries an ``after_scope_hash`` a future preflight
#: can be compared against.
#:
#: Every verb here creates or modifies. None of them deletes a project -- that
#: boundary is enforced by ``cli/policy.py`` and re-checked in ``build_parser``.
WRITE_OPS: list[tuple[str, str, Callable[[CliContext, Any], Any], tuple[str, ...]]] = [
    ("project", "create", _project_create, ()),
    ("project", "set", _project_set, ("projects",)),
    ("story", "append", _story_append, ("story_beats",)),
    ("story", "update", _story_update, ("story_beats",)),
    ("story", "replace-range", _story_replace, ("story_beats",)),
    ("cast", "upsert", _cast_upsert, ("characters", "locations", "items")),
    ("cast", "update", _cast_update, ("characters", "locations", "items")),
    ("script", "append", _script_append, ("scenes",)),
    ("script", "update", _script_update, ("scenes",)),
    ("script", "replace-range", _script_replace, ("scenes",)),
    ("clips", "append", _clips_append, ("clips",)),
    ("clips", "update", _clips_update, ("clips",)),
    ("clips", "replace-range", _clips_replace, ("clips",)),
    ("context", "set", _context_set, ("projects",)),
]

