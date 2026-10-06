"""The delete boundary: three layers, tested independently.

Layer 1 (syntax)  -- no destructive verb exists in the argparse tree.
Layer 2 (policy)  -- ``policy.check_op`` refuses one if it is ever called.
Layer 3 (tables)  -- no write op is registered under a read-only group, and no
                     read op is named destructively either.

If a future change adds a ``project drop`` subcommand, this file fails before it
ships rather than after someone uses it.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from calliope.authoring.service import PolicyError
from calliope.cli import read_ops, write_ops
from calliope.cli.main import build_parser
from calliope.cli.policy import (
    FORBIDDEN_OPS,
    FORBIDDEN_VERBS,
    READ_ONLY_GROUPS,
    check_op,
    is_forbidden_name,
)


def _walk(parser: argparse.ArgumentParser, path: tuple[str, ...] = ()):
    """Yield (command path, subparser) for every leaf in the tree."""
    subparser_actions = [
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    ]
    if not subparser_actions:
        yield path, parser
        return
    for name, sub in sorted(subparser_actions[0].choices.items()):
        yield from _walk(sub, path + (name,))


COMMANDS = dict(_walk(build_parser()))


# -- layer 1: syntax -------------------------------------------------------


def test_tree_has_commands():
    # If this is empty the walk is broken and every assertion below is vacuous.
    assert len(COMMANDS) >= 25


def test_no_destructive_verb_anywhere_in_the_tree():
    offenders = [
        " ".join(path)
        for path, _ in COMMANDS.items()
        if any(is_forbidden_name(part) for part in path)
    ]
    assert offenders == []


def test_no_delete_like_flag_anywhere():
    """Not just subcommands: no --delete / --force-delete style escape hatch."""
    offenders = []
    for path, sub in COMMANDS.items():
        for action in sub._actions:
            for opt in action.option_strings:
                lowered = opt.lower()
                if is_forbidden_name(lowered.lstrip("-")) or lowered in (
                    "--force",
                    "--yes",
                    "--no-backup",
                ):
                    offenders.append(f"{' '.join(path)} {opt}")
    assert offenders == []


# -- layer 2: policy -------------------------------------------------------


@pytest.mark.parametrize("verb", sorted(FORBIDDEN_VERBS))
def test_policy_refuses_every_forbidden_verb(verb):
    with pytest.raises(PolicyError):
        check_op("project", verb)


def test_policy_names_the_web_ui_as_the_only_delete_path():
    with pytest.raises(PolicyError) as excinfo:
        check_op("project", "delete")
    assert "web UI" in str(excinfo.value)


def test_policy_allows_normal_writes():
    for group, verb in [("story", "append"), ("clips", "update"), ("context", "set")]:
        check_op(group, verb)  # must not raise


def test_policy_refuses_a_write_under_a_read_only_group():
    for group in READ_ONLY_GROUPS:
        with pytest.raises(PolicyError):
            check_op(group, "append", writes=True)


def test_forbidden_ops_is_empty_but_present():
    # It exists as a documented extension point. The assertion is that the
    # project-delete boundary does NOT rely on it -- that is enforced by the
    # verb-name layers -- so an accidental entry cannot weaken the guarantee.
    assert isinstance(FORBIDDEN_OPS, frozenset)


# -- layer 3: the op tables ------------------------------------------------


def test_no_write_op_is_registered_under_a_read_only_group():
    offenders = [
        f"{group} {verb}"
        for group, verb, _, _ in write_ops.WRITE_OPS
        if group in READ_ONLY_GROUPS
    ]
    assert offenders == []


def test_no_op_table_entry_is_destructive():
    offenders = [
        f"{group} {verb}"
        for group, verb, _, _ in write_ops.WRITE_OPS
        if is_forbidden_name(verb)
    ] + [
        f"{group} {verb}"
        for group, verb, _ in read_ops.READ_OPS
        if is_forbidden_name(verb)
    ]
    assert offenders == []


def test_every_write_op_is_reachable_from_the_argparse_tree():
    """The dispatcher builds from the tables, so a handler that never reaches
    the tree would be dead code that still looks supported."""
    tree_verbs = {" ".join(path) for path in COMMANDS}
    missing = [
        f"{group} {verb}"
        for group, verb, _, _ in write_ops.WRITE_OPS
        if f"{group} {verb}" not in tree_verbs
    ]
    missing += [
        f"{group} {verb}"
        for group, verb, _ in read_ops.READ_OPS
        if f"{group} {verb}" not in tree_verbs
    ]
    assert missing == []


def test_no_read_op_is_flagged_as_a_write():
    write_pairs = {(g, v) for g, v, _, _ in write_ops.WRITE_OPS}
    assert {(g, v) for g, v, _ in read_ops.READ_OPS} & write_pairs == set()


# -- the web UI remains the only delete path -------------------------------


def test_audit_rows_cascade_with_the_project(tmp_path):
    """The log's rows go when the project does -- ON DELETE CASCADE, not a
    cleanup job that can be skipped."""
    import asyncio

    from calliope.db import get_db, migrate_db

    db = tmp_path / "t.db"
    asyncio.run(migrate_db(db))
    conn = get_db(db)
    pid = conn.execute("INSERT INTO projects (title) VALUES ('x')").lastrowid
    conn.execute(
        "INSERT INTO cli_audit (project_id, actor, command, summary) "
        "VALUES (?, 'cli', 'x', 'y')",
        (pid,),
    )
    conn.commit()
    conn.execute("DELETE FROM projects WHERE id = ?", (pid,))
    conn.commit()
    left = conn.execute(
        "SELECT COUNT(*) AS c FROM cli_audit WHERE project_id = ?", (pid,)
    ).fetchone()["c"]
    conn.close()
    assert left == 0


def test_delete_project_removes_the_jsonl_mirror(tmp_path, monkeypatch):
    """The mirror is on disk, so ON DELETE CASCADE cannot reach it.

    This exercises the real removal loop, including the "path does not exist"
    case: the router always returns both candidate paths even when only one is
    present, so the loop has to tolerate a missing file.
    """
    import asyncio

    import calliope.config as config_module
    from calliope.db import migrate_db
    from calliope.routers.projects import _cli_project_files

    monkeypatch.setattr(config_module.settings, "data_dir", tmp_path)
    asyncio.run(migrate_db(config_module.settings.db_path))

    mirror = tmp_path / "audit" / "project-42.jsonl"
    mirror.parent.mkdir(parents=True, exist_ok=True)
    mirror.write_text("{}\n", encoding="utf-8")

    assert set(_cli_project_files(42)) == {mirror}

    # Mirrors routers/projects.py::delete_project's post-commit loop.
    for path in _cli_project_files(42):
        path.unlink(missing_ok=True)

    assert not mirror.exists()


def test_delete_project_is_fine_with_no_audit_mirror(tmp_path, monkeypatch):
    """A project that never used the CLI has no mirror. That is not an error.

    ``delete`` must not depend on the CLI having touched the project -- the
    common case is deleting something the user created and abandoned in the UI.
    """
    import asyncio

    import calliope.config as config_module
    from calliope.db import migrate_db
    from calliope.routers.projects import _cli_project_files

    monkeypatch.setattr(config_module.settings, "data_dir", tmp_path)
    asyncio.run(migrate_db(config_module.settings.db_path))

    for path in _cli_project_files(42):
        path.unlink(missing_ok=True)

    assert all(not p.exists() for p in _cli_project_files(42))


def test_no_snapshot_directory_is_referenced_anywhere():
    """``cli_snapshots/`` is not a thing, and no source file may say it is.

    ``--snapshot`` was dropped for ``--expect-hash`` (plan §3.5.1), so the
    directory never exists on disk. A cleanup branch and a docstring that still
    name it describe a path no code can populate -- the kind of dead reference
    that reads as a live guarantee. Grepped across both packages because the
    failure mode is prose, not behaviour.
    """
    from calliope import authoring, cli

    offenders: list[str] = []
    for pkg in (cli, authoring):
        for path in sorted(Path(pkg.__file__).parent.glob("*.py")):
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if "cli_snapshots" in line:
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, "cli_snapshots/ is dead -- drop the reference:\n" + "\n".join(
        offenders
    )
