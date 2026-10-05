"""Read commands. Zero SQL lives here -- everything routes through
``calliope.authoring``.

Every command is ``(group, verb, func)``; the dispatcher in ``main.py`` builds
the argparse tree from this table so the syntax layer cannot drift from the
policy layer (``cli/policy.py`` scans that same tree in its test).
"""
from __future__ import annotations

import json
from typing import Any, Callable

from calliope.authoring import audit as audit_mod  # noqa: F401 - re-exported for ops
from calliope.authoring import beats as beats_mod
from calliope.authoring import cast as cast_mod
from calliope.authoring import context as ctx_mod
from calliope.authoring import projects as proj_mod
from calliope.authoring import script as script_mod
from calliope.authoring.hash import hash_rows, project_digests, scope_digest
from calliope.authoring.models import INPUT_MODELS, schema_for, validate_payload
from calliope.authoring.service import NotFound, ValidationFailed
from calliope.cli.context import CliContext
from calliope.cli.io import load_payload, resolve_project_arg


def _pid(ctx: CliContext, args: Any) -> int:
    return resolve_project_arg(ctx.conn, args.project)


# -- project ---------------------------------------------------------------


def _project_list(ctx: CliContext, args: Any) -> Any:
    return proj_mod.list_projects(
        ctx.conn, status=args.status, ingest_mode=args.ingest_mode
    )


def _project_show(ctx: CliContext, args: Any) -> Any:
    pid = _pid(ctx, args)
    row = proj_mod.get_project(ctx.conn, pid)
    idea = row.pop("idea", None)
    out = {
        "id": pid,
        **{k: v for k, v in row.items()},
        "source": proj_mod.idea_meta(idea),
        "counts": proj_mod.counts(ctx.conn, pid),
        "plan": ctx_mod.plan_state(ctx.conn, pid)["state"],
        # How many times the CLI has written to this project. Display only --
        # the drift guard is the content hash from `project hash`, not this
        # number, which is why HASH_EXCLUDE drops it.
        "project_revision": row.get("project_revision"),
    }
    # The source text itself is only included on explicit request. A 34k-char
    # novel does not belong in a summary an agent reads first (plan §5.0.3).
    if getattr(args, "with_idea", False):
        out["idea"] = idea
    return out


def _project_source(ctx: CliContext, args: Any) -> Any:
    return proj_mod.read_source(
        ctx.conn,
        _pid(ctx, args),
        from_chars=args.from_chars,
        to_chars=args.to_chars,
        grep=args.grep,
        context_lines=args.context_lines,
    )


def _project_hash(ctx: CliContext, args: Any) -> Any:
    """Every writable table's content fingerprint, for use as ``--expect-hash``.

    The drift guard is only meaningful if the caller can produce the value it
    compares against, so this command exists for that purpose and nothing else.
    Read it immediately before reading the scope you are about to write, and
    pass the table's value to the write.
    """
    digests = project_digests(ctx.conn, _pid(ctx, args))
    if not getattr(args, "with_ids", False):
        return digests
    pid = _pid(ctx, args)
    return {
        table: scope_digest(
            hash_rows(ctx.conn, table, project_id=pid), with_ids=True
        )
        for table in digests
    }


# -- story -----------------------------------------------------------------


def _story_list(ctx: CliContext, args: Any) -> Any:
    return beats_mod.list_beats(
        ctx.conn, _pid(ctx, args), from_index=args.from_index, to_index=args.to_index
    )


def _story_get(ctx: CliContext, args: Any) -> Any:
    return beats_mod.get_beat(ctx.conn, _pid(ctx, args), args.beat_id)


# -- cast ------------------------------------------------------------------


def _cast_list(ctx: CliContext, args: Any) -> Any:
    return cast_mod.list_cast(ctx.conn, _pid(ctx, args))


# -- script / clips --------------------------------------------------------


def _script_list(ctx: CliContext, args: Any) -> Any:
    return script_mod.list_scenes(
        ctx.conn, _pid(ctx, args), from_index=args.from_index, to_index=args.to_index
    )


def _script_get(ctx: CliContext, args: Any) -> Any:
    scene = script_mod.get_scene(ctx.conn, _pid(ctx, args), args.scene_id)
    return {**scene, "clips": [_hydrate_clip(c) for c in scene.get("clips") or []]}


def _clips_list(ctx: CliContext, args: Any) -> Any:
    if getattr(args, "scene_id", None) is not None:
        clips = script_mod.list_clips(ctx.conn, int(args.scene_id))
    else:
        clips = script_mod.list_all_clips(ctx.conn, _pid(ctx, args))
    return [_hydrate_clip(clip) for clip in clips]


def _hydrate_clip(clip: dict[str, Any]) -> dict[str, Any]:
    """Decode the JSON-in-TEXT columns the web UI also decodes.

    ``clips.dialog_lines_covered`` is ``json.dumps`` of a list stored in a TEXT
    column (``coverage_agent.py:288``), and the API hands it back as a list
    (``routers/scenes.py:36-43``). The CLI writes a list and would otherwise
    read back a string, so an agent that patched a clip and re-read it would
    conclude the write failed.

    Only the columns the CLI itself writes are decoded. ``video_settings_json``
    is left raw for the same reason it is not in ``CLI_CLIP_FIELDS``: the CLI
    never produces it, so there is no read-after-write contract to keep.

    This is presentation only, on a copy. ``basis_hash`` fingerprints the raw
    TEXT (``continuity.py:252``), so decoding inside ``authoring`` would make
    the CLI's staleness verdict disagree with the recalculation path's.
    """
    out = dict(clip)
    raw = out.get("dialog_lines_covered")
    if raw in (None, ""):
        out["dialog_lines_covered"] = None
    elif isinstance(raw, str):
        try:
            out["dialog_lines_covered"] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            # A row written by something other than the API. Report it as
            # absent rather than raising: the read face must never fail on
            # pre-existing data it did not create.
            out["dialog_lines_covered"] = None
    return out


# -- context / shots / plan ------------------------------------------------


def _context_get(ctx: CliContext, args: Any) -> Any:
    state = ctx_mod.plan_state(ctx.conn, _pid(ctx, args))
    return {
        "state": state["state"],
        "stale": state["stale"],
        # Two different questions. `authored` is whether the ledger has writable
        # content -- the CLI's half, and what `plan next` gates on. `stale` is
        # whether the *derived* shots still match the board, which needs
        # `ensure_continuity_plan` and therefore the model. Reporting only
        # `stale` made a correctly-authored ledger look like unfinished work.
        "authored": state["authored"],
        "basis_hash": state["basis_hash"],
        "stored_based_on": state["stored_based_on"],
        "overview": (state["plan"] or {}).get("overview") or {},
        "requirements": (state["plan"] or {}).get("requirements") or {},
    }


def _shots_list(ctx: CliContext, args: Any) -> Any:
    """Read the derived per-clip shots. Read-only by policy."""
    pid = _pid(ctx, args)
    state = ctx_mod.plan_state(ctx.conn, pid)
    plan = state["plan"] or {}
    shots = plan.get("shots") or []
    if getattr(args, "carry", False):
        return [
            {
                "clip_id": s.get("clip_id"),
                "carry": script_mod.carry_of(ctx.conn, pid, s.get("clip_id")),
            }
            for s in shots
            if isinstance(s, dict)
        ]
    return shots


def _plan_next(ctx: CliContext, args: Any) -> Any:
    from calliope.cli.plan import next_step

    return next_step(ctx.conn, _pid(ctx, args))


# -- log -------------------------------------------------------------------


def _log_list(ctx: CliContext, args: Any) -> Any:
    pid = _pid(ctx, args) if getattr(args, "project", None) else None
    return audit_mod.list_entries(
        ctx.conn,
        project_id=pid,
        command=getattr(args, "command_filter", None),
        limit=args.limit,
        since_id=getattr(args, "since_id", None),
    )


def _log_show(ctx: CliContext, args: Any) -> Any:
    return audit_mod.get_entry(ctx.conn, args.entry_id)


# -- schema ----------------------------------------------------------------


def _schema_show(ctx: CliContext, args: Any) -> Any:
    """The JSON Schema for a payload shape, straight from the Pydantic model.

    Accepts ``schema show`` / ``schema show GROUP`` / ``schema show GROUP VERB``,
    as positionals or as ``--group``/``--verb``. An unknown name raises rather
    than returning ``{"error": ...}`` with exit 0: a caller scripting against
    this would read the error object as a schema and proceed with nothing.
    """
    group, verb = args.group, args.verb
    target = getattr(args, "target", None) or []
    if len(target) > 2:
        raise ValidationFailed(
            "schema show takes at most GROUP and VERB",
            [{"loc": "target", "msg": f"got {len(target)} arguments"}],
        )
    if target:
        group = target[0]
    if len(target) > 1:
        verb = target[1]

    if group and verb:
        entry = schema_for(group, verb)
        if entry is None:
            raise NotFound(
                f"no schema for {group} {verb}; "
                f"verbs here are {sorted(INPUT_MODELS.get(group, {}))}"
            )
        return entry
    if group:
        if group not in INPUT_MODELS:
            raise NotFound(
                f"unknown group {group!r}; groups are {sorted(INPUT_MODELS)}"
            )
        return {group: sorted(INPUT_MODELS[group])}
    return {g: sorted(verbs) for g, verbs in sorted(INPUT_MODELS.items())}


def _schema_validate(ctx: CliContext, args: Any) -> Any:
    """Validate a payload against the model without writing anything."""
    payload = load_payload(args.file)
    rows = validate_payload(args.group, args.verb, payload)
    return {
        "ok": True,
        "group": args.group,
        "verb": args.verb,
        "rows": len(rows),
        "echo": [r.model_dump() for r in rows],
    }


#: (group, verb, handler). ``handler(ctx, args) -> Any``.
#: Verb names here must not appear in ``policy.FORBIDDEN_VERBS``; the boundary
#: test walks the built argparse tree to enforce it.
READ_OPS: list[tuple[str, str, Callable[[CliContext, Any], Any]]] = [
    ("project", "list", _project_list),
    ("project", "show", _project_show),
    ("project", "source", _project_source),
    ("project", "hash", _project_hash),
    ("story", "list", _story_list),
    ("story", "get", _story_get),
    ("cast", "list", _cast_list),
    ("script", "list", _script_list),
    ("script", "get", _script_get),
    ("clips", "list", _clips_list),
    ("context", "get", _context_get),
    ("shots", "list", _shots_list),
    ("plan", "next", _plan_next),
    ("log", "list", _log_list),
    ("log", "show", _log_show),
    ("schema", "show", _schema_show),
    ("schema", "validate", _schema_validate),
]

