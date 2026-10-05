"""``calliope-cli`` entry point and the argparse tree.

Three layers of enforcement meet here (plan §3.4):

- The tree is generated from the op tables in ``read_ops``/``write_ops``, so a
  subcommand cannot exist without a handler and a handler cannot hide.
- No destructive verb name is ever produced: ``policy.FORBIDDEN_VERBS`` is
  checked while building, not just at dispatch, so the failure is at parse time
  with a readable message instead of a traceback.
- Dispatch re-checks via ``cli_txn`` -> ``check_op``, because the policy layer
  should not depend on the tree being the only way in.

Exit codes are documented in ``cli.io`` and are a contract with the opencode
skill.
"""
from __future__ import annotations

import argparse
import sys
from typing import Any, Sequence

from calliope.authoring.models import INPUT_MODELS
from calliope.authoring.service import ValidationFailed
from calliope.cli import read_ops, write_ops
from calliope.cli.context import CliContext, emit
from calliope.cli.io import (
    EXIT_OK,
    add_common,
    exit_code_for,
    problem_report,
    read_idea_file,
)
from calliope.cli.policy import FORBIDDEN_VERBS, check_op

PROG = "calliope-cli"


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Read and write a Calliope project's content. "
            "The CLI can create and modify; it can never delete a project. "
            "It never calls the model -- rendering stays in the web UI."
        ),
    )
    add_common(root)
    groups = root.add_subparsers(dest="group", required=True)

    # One subparser per group, shared by its read and write verbs. argparse
    # refuses a duplicate name, so the group parser has to be created once and
    # reused -- which also means a group cannot end up with two `--help` blocks.
    group_parsers: dict[str, argparse.ArgumentParser] = {}

    def group_parser(name: str) -> argparse.ArgumentParser:
        if name not in group_parsers:
            group_parsers[name] = groups.add_parser(
                name, help=f"{name} commands"
            )
            group_parsers[name].add_subparsers(dest="verb", required=True)
        return group_parsers[name]

    def verbs_of(g: argparse.ArgumentParser) -> Any:
        for action in g._actions:
            if isinstance(action, argparse._SubParsersAction):
                return action
        raise AssertionError("group parser has no verb subparser")  # pragma: no cover

    for group, verb, handler, _ in write_ops.WRITE_OPS:
        _check_name(verb)
        v = verbs_of(group_parser(group)).add_parser(verb)
        add_common(v)
        _add_write_args(v, group, verb)
        v.set_defaults(_handler=handler, _group=group, _verb=verb, _write=True)

    for group, verb, handler in read_ops.READ_OPS:
        _check_name(verb)
        v = verbs_of(group_parser(group)).add_parser(verb)
        add_common(v)
        _add_read_args(v, group, verb)
        v.set_defaults(_handler=handler, _group=group, _verb=verb, _write=False)

    return root


def _check_name(verb: str) -> None:
    if verb.lower() in FORBIDDEN_VERBS:
        # A programming error, not a user error: fail at import-time-of-tree.
        raise AssertionError(
            f"refusing to register destructive verb {verb!r} (policy.FORBIDDEN_VERBS)"
        )


def _add_write_args(p: argparse.ArgumentParser, group: str, verb: str) -> None:
    if verb == "create":
        p.add_argument("--title", required=True)
        p.add_argument(
            "--idea",
            default=None,
            help="the novel source text; single-line only when invoked through "
                 "calliope-cli.bat -- use --idea-file on project set for a "
                 "multi-line text, or a raw newline truncates the command line",
        )
        p.add_argument("--genre", default=None)
        p.add_argument("--tone", default=None)
        p.add_argument("--target-duration", default=None)
        p.add_argument("--ingest-mode", default="builtin",
                       choices=["builtin", "external"])
        return

    if group == "project":
        p.add_argument("--project", required=True)
        if verb == "set":
            p.add_argument("--expect-hash", default=None)
            p.add_argument("--title", default=None)
            p.add_argument("--idea", default=None,
                           help="single-line only via calliope-cli.bat; a raw "
                                "newline truncates the rest of the command line")
            p.add_argument("--idea-file", default=None,
                           help="read the source text from a file ('-' for stdin) "
                                "-- the only safe way to write a multi-line novel")
            p.add_argument("--genre", default=None)
            p.add_argument("--tone", default=None)
            p.add_argument("--target-duration", default=None)
            p.add_argument("--status", default=None)
            p.add_argument("--cover-path", default=None)
        return

    p.add_argument("--project", required=True)
    if verb != "create":
        # Preflight fingerprint from the caller's last read. Optional because a
        # caller that deliberately skips the read should still be able to write;
        # the discipline is the caller's, and `project hash` is what makes
        # supplying it cheap. See cli/write_ops.py::_guard.
        p.add_argument("--expect-hash", default=None,
                       help="scope fingerprint from `project hash`; refuses to "
                            "write if the rows moved since your read")
    p.add_argument("--file", default=None,
                   help="JSON payload ('-' reads stdin)")

    if verb == "append":
        p.add_argument("--scene", default=None, help="clips only: position or id")
    if verb == "update":
        if group == "story":
            p.add_argument("--beat-id", required=True)
            p.add_argument("--title", default=None)
            p.add_argument("--description", default=None)
            p.add_argument("--order-index", type=int, default=None)
        elif group == "cast":
            p.add_argument("--entity-id", required=True,
                           help="row id, or the exact name")
            p.add_argument("--kind", default="character",
                           choices=["character", "location", "item"])
            p.add_argument("--role", default=None)
            p.add_argument("--age", default=None)
            p.add_argument("--appearance", default=None)
            p.add_argument("--personality", default=None)
            p.add_argument("--description", default=None)
            p.add_argument("--consistency-prompt", default=None)
            p.add_argument("--reference-image-path", default=None)
            p.add_argument("--portrait-path", default=None)
            p.add_argument("--sheet-path", default=None)
        elif group == "script":
            p.add_argument("--scene-id", required=True)
            p.add_argument("--heading", default=None)
            p.add_argument("--action", default=None)
            p.add_argument("--dialog", default=None)
            p.add_argument("--duration-sec", type=float, default=None)
            p.add_argument("--order-index", type=int, default=None)
            p.add_argument("--location-id", type=int, default=None)
        elif group == "clips":
            p.add_argument("--clip-id", required=True)
            p.add_argument("--description", default=None)
            p.add_argument("--shot-size", default=None)
            p.add_argument("--duration-sec", type=float, default=None)
            p.add_argument("--order-index", type=int, default=None)
            p.add_argument("--dialog-lines-covered", default=None,
                           help="comma-separated 1-based line numbers")
        return

    if verb == "replace-range":
        p.add_argument("--from-index", type=int, required=True)
        p.add_argument("--to-index", type=int, required=True)
        p.add_argument("--scene", default=None, help="clips only: position or id")

    if group == "cast" and verb == "upsert":
        p.add_argument("--kind", default=None,
                       help="override the kind of every row in the payload")


def _add_read_args(p: argparse.ArgumentParser, group: str, verb: str) -> None:
    if verb == "validate":
        p.add_argument("--group", required=True)
        p.add_argument("--verb", required=True)
        p.add_argument("--file", required=True)
        return
    if group == "schema":
        p.add_argument("--group", default=None)
        p.add_argument("--verb", default=None)
        if verb == "show":
            # `schema show story append` instead of `--group story --verb append`.
            # Two positionals, because the pair is one thing in prose ("the
            # story append payload") and the flags make an agent guess dashes.
            p.add_argument(
                "target",
                nargs="*",
                metavar="GROUP VERB",
                help="select one group, or one group+verb, without --group/--verb",
            )
        return
    if group == "log" and verb == "list":
        p.add_argument("--project", default=None)
        p.add_argument("--command-filter", default=None)
        p.add_argument("--limit", type=int, default=50)
        p.add_argument("--since-id", type=int, default=None)
        return
    if group == "log" and verb == "show":
        p.add_argument("--entry-id", type=int, required=True)
        return

    p.add_argument("--project", default=None)
    if group == "project" and verb == "list":
        p.add_argument("--status", default=None)
        p.add_argument("--ingest-mode", default=None)
        return
    if group == "project" and verb == "show":
        p.add_argument("--with-idea", action="store_true",
                       help="include the full source text (usually huge)")
        return
    if group == "project" and verb == "source":
        p.add_argument("--from-chars", type=int, default=None)
        p.add_argument("--to-chars", type=int, default=None)
        p.add_argument("--grep", default=None)
        p.add_argument("--context-lines", type=int, default=2)
        return
    if group == "project" and verb == "hash":
        p.add_argument("--with-ids", action="store_true",
                       help="fold row ids into each fingerprint (easier to "
                            "reconcile by hand against `log show`)")
        return
    if group == "story":
        if verb == "list":
            p.add_argument("--from-index", type=int, default=None)
            p.add_argument("--to-index", type=int, default=None)
        else:
            p.add_argument("--beat-id", type=int, required=True)
        return
    if group == "script":
        if verb == "list":
            p.add_argument("--from-index", type=int, default=None)
            p.add_argument("--to-index", type=int, default=None)
        else:
            p.add_argument("--scene-id", type=int, required=True)
        return
    if group == "clips":
        p.add_argument("--scene-id", type=int, default=None,
                       help="one scene, or omit for the whole project")
        return
    if group == "shots":
        p.add_argument("--carry", action="store_true",
                       help="show the [CARRY] line from each clip's description")
        return


def _all_option_strings(parser: argparse.ArgumentParser) -> set[str]:
    """Every option string anywhere in the tree, at every level."""
    out: set[str] = set()
    for action in parser._actions:
        out.update(action.option_strings)
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                out |= _all_option_strings(sub)
    return out


def _patch_from_args(args: Any, model: type) -> Any:
    """Build a Pydantic patch from whatever flags were actually passed.

    ``exclude_unset`` semantics matter: a flag that was not given must not
    appear in the model at all, or ``description=None`` would be written over
    real content.
    """
    given = getattr(args, "_given", ())
    data = {}
    for key in model.model_fields:
        if "--" + key.replace("_", "-") not in given:
            continue
        value = getattr(args, key, None)
        if key == "dialog_lines_covered" and isinstance(value, str):
            # "1,2" on the command line -> [1, 2] for the validator, so an
            # out-of-range or zero index is caught here rather than as a
            # silently wrong list.
            value = [
                int(part) for part in value.replace(",", " ").split() if part
            ]
        data[key] = value
    return model.model_validate(data)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Which flags the caller actually typed. Needed because argparse cannot
    # distinguish "flag omitted" from "flag given as None" -- and for an update
    # those mean very different things: omitting --description must leave the
    # stored description alone, while passing it empty must clear it.
    #
    # The option strings have to be collected from the WHOLE tree, not just the
    # root parser: --title lives on `story update`, several levels down.
    argv = list(argv) if argv is not None else sys.argv[1:]
    known = _all_option_strings(parser)
    given = {tok.split("=", 1)[0] for tok in argv if tok.split("=", 1)[0] in known}

    handler = getattr(args, "_handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_OK

    # getattr, not attribute access: --json / --dry-run are declared with
    # default=SUPPRESS (see cli/io.py::add_common), so an absent flag leaves no
    # attribute at all rather than a False that would clobber the root parser's
    # value when the flag was written before the command path.
    as_json = getattr(args, "json", False)
    ctx = CliContext(dry_run=getattr(args, "dry_run", False))
    try:
        check_op(args._group, args._verb, writes=getattr(args, "_write", False))
        args._given = given
        # Named `patch` (not `_patch`) because that is what every write op
        # reaches for; keeping one spelling avoids a silently-unset attribute.
        args.patch = _patch(args)
        result = handler(ctx, args)
        emit(result, as_json=as_json)
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - this is the CLI's error boundary
        # Deliberately not BaseException: SystemExit (argparse refusing an
        # unknown command) and KeyboardInterrupt must reach the shell with their
        # own exit codes, not be converted into exit 1 with a message.
        code = exit_code_for(exc)
        if as_json and hasattr(exc, "problems"):
            emit(problem_report(exc), as_json=True, stream=sys.stderr)
        else:
            sys.stderr.write(f"error: {exc}\n")
            if hasattr(exc, "problems"):
                for problem in exc.problems[:20]:
                    for detail in problem.get("problems", [problem]):
                        sys.stderr.write(f"  {detail.get('loc')}: {detail.get('msg')}\n")
        return code
    finally:
        ctx.close()


def _patch(args: Any) -> Any:
    """Build the Pydantic model for a flag-driven verb, or None for the rest.

    Driven by ``authoring.models.INPUT_MODELS`` rather than a table written out
    here, so a verb cannot acquire flags without also acquiring the model that
    validates them: it is the same map ``schema show`` and ``validate_payload``
    read. A hardcoded dict had already drifted -- ``project set`` has flags and a
    model but was not in it, so ``args.patch`` came back None and the command died
    on ``None.model_dump``.

    ``accepts == "none"`` is what marks a flag verb: the others take ``--file``
    (or, for ``context set``, a whole object).
    """
    entry = INPUT_MODELS.get(args._group, {}).get(args._verb)
    if entry is None:
        return None
    model, accepts = entry
    if accepts != "none":
        return None
    if args._group == "project" and args._verb == "set":
        _resolve_idea_file(args)
    return _patch_from_args(args, model)


def _resolve_idea_file(args: Any) -> None:
    """``--idea-file`` is a way to spell ``--idea``, not a column.

    cmd.exe cannot carry a raw newline inside an argument -- ``calliope-cli.bat
    --idea "<novel>"`` truncates at the first newline and silently drops the rest
    of the command line -- so a multi-line novel has to arrive through a file or
    stdin. Reading it here keeps ``ProjectPatch`` a description of *columns*
    (``idea``) rather than of transports.

    Synthesises ``--idea`` into ``args._given`` so the ordinary patch builder
    picks it up; there is no second code path for "the field was set".
    """
    path = getattr(args, "idea_file", None)
    if path is None:
        return
    if "--idea" in getattr(args, "_given", ()):
        raise ValidationFailed(
            "--idea and --idea-file both name the same column; give only one",
            [{"loc": "idea_file", "msg": "--idea was also given"}],
        )
    args.idea = read_idea_file(path)
    args._given = (*args._given, "--idea")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())