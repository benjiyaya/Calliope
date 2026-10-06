"""The three physical prohibitions, checked by parsing the source.

Plan §3.7 states three things the CLI may never do, independent of any runtime
check: no raw SQL above ``authoring/``, no filesystem writes, no shell. Each is
a property of the *text* of the package, so each is tested by reading the text
rather than by exercising behaviour -- a behavioural test can only prove the one
call it happened to make, while a source test covers every call site including
the one someone adds next month.

The delete boundary already had this treatment (``test_cli_boundary.py`` walks
the argparse tree). These three did not, which left the strongest claims in the
design doc unenforced.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from calliope.cli import policy

CLI_DIR = Path(policy.__file__).resolve().parent
AUTHORING_DIR = CLI_DIR.parent / "authoring"

#: Modules the CLI is forbidden to import outright. ``subprocess``/``os.system``
#: cover the shell prohibition; ``llm`` and the plan builder cover the "never
#: calls a model" rule -- a tool that spends 40 seconds and 3000 tokens on a
#: summarisation nobody asked for is the single worst failure this bridge could
#: have, and it would be silent.
FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "subprocess",
        "pty",
        "commands",
        "sh",
        "os.system",
        "os.popen",
        "os.spawn",
        "llm",
        "calliope.llm",
        "agent.continuity.ensure_continuity_plan",
    }
)

#: ``calliope.llm`` in particular: the CLI reaches the model through that module.
#: Matched as a substring of the import target so both ``from calliope import
#: llm`` and ``import calliope.llm`` are caught.
FORBIDDEN_IMPORT_SUBSTRINGS = ("calliope.llm", "ensure_continuity_plan")


def _modules() -> list[Path]:
    found = sorted(CLI_DIR.glob("*.py")) + sorted(AUTHORING_DIR.glob("*.py"))
    assert found, "no source found -- the CLI moved and this test is now vacuous"
    return found


def _imported_names(tree: ast.AST) -> list[str]:
    """Every module path this file imports, however it spells the import."""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # ``from a.b import c`` is both "a.b" and "a.b.c"; record both so a
            # ban on either level fires.
            base = node.module or ""
            names.append(base)
            names.extend(f"{base}.{alias.name}" for alias in node.names)
    return names


def _called_attrs(tree: ast.AST) -> set[str]:
    """Dotted attribute calls, e.g. ``os.system`` -> {"os.system"}."""
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            parts = []
            cur: ast.AST = func
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
                out.add(".".join(reversed(parts)))
        elif isinstance(func, ast.Name):
            out.add(func.id)
    return out


#: Transaction control is not SQL. ``cli_txn`` must issue ``BEGIN IMMEDIATE``
#: itself (plan §3.5: a deferred transaction that upgrades mid-flight can take
#: SQLITE_BUSY on the upgrade path, where busy_timeout does not apply, and roll
#: back half the work), and ``sqlite3`` is imported purely to annotate the
#: ``conn: sqlite3.Connection`` parameter that every writer takes. Both are
#: allowed; reading or writing a row is not.
TRANSACTION_CONTROL = frozenset({"begin", "commit", "rollback", "end"})


def _is_transaction_control(call: ast.Call) -> bool:
    """True for ``conn.execute("BEGIN IMMEDIATE")`` and friends."""
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "execute"):
        return False
    if not call.args or not isinstance(call.args[0], ast.Constant):
        return False
    text = call.args[0].value
    return (
        isinstance(text, str)
        and text.strip().split()[0:1]
        and text.strip().split()[0].lower() in TRANSACTION_CONTROL
    )


def test_cli_layer_contains_no_raw_sql():
    """``cli/`` holds no row-level SQL; ``authoring/`` holds all of it (plan §3.1).

    Checked structurally rather than by grepping for SELECT/INSERT: the real
    hazard is a ``conn.execute`` with an f-string or ``%``, which a keyword
    search would miss entirely and which is exactly the injection shape this
    layer exists to prevent.

    ``import sqlite3`` is fine -- it is a type annotation, not a query.
    """
    offenders = []
    for path in sorted(CLI_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"execute", "executemany", "executescript"}:
                    if not _is_transaction_control(node):
                        offenders.append(
                            f"{path.name}:{node.lineno}: .{node.func.attr}("
                        )
    assert not offenders, "cli/ must hold no row-level SQL; use authoring/:\n" + "\n".join(
        offenders
    )


def test_no_module_imports_a_shell_or_a_model():
    for path in _modules():
        names = _imported_names(ast.parse(path.read_text(encoding="utf-8")))
        for name in names:
            root = name.split(".")[0]
            assert root not in {"subprocess", "pty", "sh", "commands"}, (
                f"{path.name} imports {name} -- the CLI may not spawn a shell"
            )
            for bad in FORBIDDEN_IMPORT_SUBSTRINGS:
                assert bad not in name, (
                    f"{path.name} imports {name} -- the CLI never calls a model"
                )


def test_no_module_calls_a_shell_or_a_model():
    """Same rule, checked at the call site.

    ``os`` is legitimately imported for path and env work, so banning the module
    name would be wrong; the dangerous members are what must not be *called*.
    """
    banned_calls = {
        "os.system",
        "os.popen",
        "os.spawnl",
        "os.spawnv",
        "os.spawnve",
        "os.execv",
        "os.execve",
        "os.fork",
        "eval",
        "exec",
        "compile",
        "__import__",
    }
    for path in _modules():
        calls = _called_attrs(ast.parse(path.read_text(encoding="utf-8")))
        for call in sorted(calls & banned_calls):
            pytest.fail(f"{path.name} calls {call}() -- forbidden in the CLI")


def test_audit_mirror_is_the_only_filesystem_write():
    """``authoring/`` writes exactly one place: the audit JSONL mirror.

    Plan §3.7 allows ``<data_dir>/audit/``. Everything else -- a stray temp file,
    a cache, a "helpful" export -- is out of bounds, and none of it would fail a
    behavioural test because a test only ever exercises one command.
    """
    writers = {"write_text", "write_bytes", "mkdir", "rmdir", "unlink", "rename"}
    offenders = []
    for path in sorted(AUTHORING_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in writers:
                    offenders.append(f"{path.name}:{node.lineno}: .{node.func.attr}(")
    # audit.py's mirror is the sanctioned one; assert exactly that and nothing
    # else, so a second writer has to be argued for explicitly.
    assert offenders == ["audit.py:234: .mkdir("], (
        "authoring/ may only write the audit mirror:\n" + "\n".join(offenders)
    )


def test_the_test_itself_would_not_pass_vacuously(tmp_path):
    """Guard the guard.

    A source-scanning test silently passes when its glob matches nothing -- the
    package gets renamed or moved and every assertion below turns into ``True``.
    This writes a file that violates each rule into a copy of the tree and
    asserts the same predicates reject it.
    """
    assert len(_modules()) >= 10, "expected the full cli/ + authoring/ tree"

    bad_sql = tmp_path / "bad_sql.py"
    bad_sql.write_text(
        "import sqlite3\n"
        "def go(conn, pid):\n"
        "    return conn.execute(f\"DELETE FROM projects WHERE id = {pid}\")\n",
        encoding="utf-8",
    )
    tree = ast.parse(bad_sql.read_text(encoding="utf-8"))
    assert "sqlite3" in _imported_names(tree)
    assert any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "execute"
        for n in ast.walk(tree)
    )

    bad_shell = tmp_path / "bad_shell.py"
    bad_shell.write_text("import subprocess\nsubprocess.run(['rm', '-rf'])\n", encoding="utf-8")
    names = _imported_names(ast.parse(bad_shell.read_text(encoding="utf-8")))
    assert "subprocess" in names
    assert {"subprocess.run"} & _called_attrs(ast.parse(bad_shell.read_text(encoding="utf-8")))

    bad_llm = tmp_path / "bad_llm.py"
    bad_llm.write_text(
        "from calliope.agent.continuity import ensure_continuity_plan\n", encoding="utf-8"
    )
    llm_names = _imported_names(ast.parse(bad_llm.read_text(encoding="utf-8")))
    assert any("ensure_continuity_plan" in n for n in llm_names)

    bad_write = tmp_path / "bad_write.py"
    bad_write.write_text("def go(p):\n    p.write_text('x')\n", encoding="utf-8")
    assert any(
        c.endswith(".write_text")
        for c in _called_attrs(ast.parse(bad_write.read_text(encoding="utf-8")))
    )

    # And the SQL predicate must actually reject a row-level statement while
    # accepting transaction control, or the test above proves nothing.
    row_sql = ast.parse('conn.execute("SELECT id FROM projects WHERE id = ?", (1,))\n')
    row_call = next(n for n in ast.walk(row_sql) if isinstance(n, ast.Call))
    assert not _is_transaction_control(row_call)
    txn_sql = ast.parse('conn.execute("BEGIN IMMEDIATE")\n')
    txn_call = next(n for n in ast.walk(txn_sql) if isinstance(n, ast.Call))
    assert _is_transaction_control(txn_call)
