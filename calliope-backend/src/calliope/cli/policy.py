"""The delete boundary, in three independent layers.

A project delete removes the user\'s novel source text, every beat, every clip,
every render, and the audit log. That is the one irreversible thing in the
system, so it is fenced three ways that fail independently (plan §3.4):

1. **Syntax.** No subcommand in this package is named ``delete``, ``remove``,
   ``rm``, ``drop``, ``purge``, ``destroy``, or ``truncate``. There is nothing
   to type.
2. **Policy.** ``FORBIDDEN_OPS`` below, checked against the parsed verb pair
   before dispatch. Catches a verb that *looks* harmless but routes into a
   destructive code path.
3. **Tests.** ``tests/test_cli_boundary.py`` walks the whole argparse tree and
   asserts none of the forbidden names appear at any level -- so a future
   subcommand cannot quietly reintroduce one.

There is no flag, env var, or config key that turns any layer off. The only way
to delete a project is the web UI, which is also the only place that cleans up
its audit mirror (``delete_project`` in ``routers/projects.py``).

``replace-range`` is not a loophole: it removes rows, but only inside one
project\'s sequence, only as part of an insert that replaces them in the same
transaction, and every removed row's before-image is recorded in the audit entry
(``log show --entry-id``) so the change can be reconstructed. It cannot empty a
group, because an empty replacement payload is rejected outright.
"""
from __future__ import annotations

from calliope.authoring.service import PolicyError

#: Subcommand names that must never exist. Matched case-insensitively against
#: every level of the command path.
FORBIDDEN_VERBS = frozenset(
    {
        "delete",
        "remove",
        "rm",
        "drop",
        "purge",
        "destroy",
        "truncate",
        "erase",
        "wipe",
        "destroy-all",
    }
)

#: (group, verb) pairs refused even though neither word looks destructive.
#: ``context set`` clears the continuity ledger; ``clips replace-range`` with a
#: huge range can empty a scene. Both stay writable -- they are the authoring
#: path -- but a policy entry documents the intent so a future reader does not
#: have to re-derive it.
FORBIDDEN_OPS: frozenset[tuple[str, str]] = frozenset()

#: Groups whose verbs are all reads. This is an ASSERTION about the op tables,
#: not a runtime block: ``tests/test_cli_boundary.py`` cross-checks it against
#: ``write_ops.WRITE_OPS`` so a write verb cannot appear under one of these
#: names by accident. Refusing at dispatch instead would just break the read
#: commands that are the point of these groups.
READ_ONLY_GROUPS = frozenset({"log", "schema", "plan", "shots"})


def check_op(group: str, verb: str, *, writes: bool = False) -> None:
    """Refuse a forbidden command. Raises PolicyError (exit code 5).

    ``writes`` is the caller's claim about whether this op mutates. A write verb
    under a read-only group name is a policy violation, not a typo to pass
    through.
    """
    low_verb = verb.lower()
    if low_verb in FORBIDDEN_VERBS:
        raise PolicyError(
            f"{group} {verb} is forbidden: the CLI may create and modify "
            "projects, never delete them. Use the web UI to delete a project."
        )
    if (group, verb) in FORBIDDEN_OPS:
        raise PolicyError(f"{group} {verb} is forbidden by policy")
    if writes and group in READ_ONLY_GROUPS:
        raise PolicyError(
            f"{group} is a read-only group; {group} {verb} may not write. "
            "Add it to policy.READ_ONLY_GROUPS' complement only if it is "
            "genuinely authoring."
        )


def is_forbidden_name(name: str) -> bool:
    """True if ``name`` is a destructive verb. Used by the boundary test."""
    return name.lower() in FORBIDDEN_VERBS
