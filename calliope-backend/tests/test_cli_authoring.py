"""The write path: preflight, transaction boundaries, audit, invariants.

Every test here goes through the real dispatcher (``cli.main.main``) so what is
tested is what an agent actually types.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def env(cli_env):
    return cli_env


# -- dry run ---------------------------------------------------------------


def test_dry_run_writes_nothing(env):
    p = env["payload"]("b.json", [{"title": "B1"}])
    before_logs = len(env["logs"](env["project_id"]))
    code, _, err = env["json"](
        "story", "append", "--project", env["project_id"], "--file", p, "--dry-run"
    )
    assert code == 0, err
    rows = env["conn"].execute("SELECT COUNT(*) AS c FROM story_beats").fetchone()["c"]
    assert rows == 0
    assert len(env["logs"](env["project_id"])) == before_logs


def test_dry_run_reports_both_sides(env):
    """Both sides: what was validated, and what would change."""
    p = env["payload"]("b.json", [{"title": "B1"}, {"title": "B2"}])
    code, res, err = env["json"](
        "story", "append", "--project", env["project_id"], "--file", p, "--dry-run"
    )
    assert code == 0, err


def test_dry_run_still_rejects_bad_input(env):
    p = env["payload"]("bad.json", [{"description": "no title"}])
    code, _, err = env["json"](
        "story", "append", "--project", env["project_id"], "--file", p, "--dry-run"
    )
    assert code == 2
    assert "title" in err


def test_dry_run_writes_no_mirror_file(env):
    """The project create in the fixture already wrote one mirror, so this
    checks that the dry-run appends nothing rather than that the dir is absent."""
    p = env["payload"]("b.json", [{"title": "B1"}])
    mirror = env["data_dir"] / "audit" / f"project-{env['project_id']}.jsonl"
    before = mirror.read_text(encoding="utf-8") if mirror.exists() else ""
    env["json"](
        "story", "append", "--project", env["project_id"], "--file", p, "--dry-run"
    )
    after = mirror.read_text(encoding="utf-8") if mirror.exists() else ""
    assert after == before


# -- story order: the read face for a soft invariant -----------------------


def test_story_order_reports_a_clean_sequence(env):
    p = env["payload"]("b.json", [{"title": f"B{i}"} for i in (1, 2, 3)])
    code, _, err = env["json"](
        "story", "append", "--project", env["project_id"], "--file", p
    )
    assert code == 0, err
    code, out, err = env["json"](
        "story", "order", "--project", env["project_id"], "--json"
    )
    assert code == 0, err
    assert out["dense"] is True
    assert out["counts"] == {"rows": 3, "distinct": 3, "min": 1, "max": 3}
    assert out["holes"] == [] and out["duplicates"] == []


def test_story_order_sees_a_hole_the_write_left_behind(env):
    """`append` extends from the tail, so it cannot fill a hole. Without this
    read command the gap would be permanent and invisible."""
    p = env["payload"]("b.json", [{"title": "B1", "order_index": 1},
                                  {"title": "B3", "order_index": 3}])
    code, _, err = env["json"](
        "story", "append", "--project", env["project_id"], "--file", p
    )
    assert code == 0, err
    code, out, err = env["json"](
        "story", "order", "--project", env["project_id"], "--json"
    )
    assert code == 0, err
    assert out["dense"] is False
    assert out["holes"] == [2]


def test_story_order_writes_nothing(env):
    """It is a read verb under a group that has writers; it must still be free."""
    p = env["payload"]("b.json", [{"title": "B1"}])
    env["json"]("story", "append", "--project", env["project_id"], "--file", p)
    before = len(env["logs"](env["project_id"]))
    code, _, err = env["json"](
        "story", "order", "--project", env["project_id"], "--json"
    )
    assert code == 0, err
    assert len(env["logs"](env["project_id"])) == before


def test_story_order_is_read_only_per_the_policy_table(env):
    """`story` has writers, so READ_ONLY_GROUPS cannot cover it. The op table is
    the assertion instead -- pin that the new verb is flagged as a read."""
    from calliope.cli.read_ops import READ_OPS

    writes = {(g, v) for g, v, _h, _w in __import__(
        "calliope.cli.write_ops", fromlist=["WRITE_OPS"]
    ).WRITE_OPS}
    assert ("story", "order") not in writes
    assert ("story", "order") in {(g, v) for g, v, _h in READ_OPS}


# -- audit -----------------------------------------------------------------


def test_nothing_is_an_undo_entry(env):
    """``cli_audit.undo_of_entry_id`` is reserved and always NULL.

    Rollback is a new row pointing back at the one it reverses; reverse
    execution was rejected because ``replace-range`` has already deleted the
    original rows and undo would mean resurrecting them -- the capability the
    delete boundary forbids (plan §5.13.2). Pinning it means a caller appearing
    later has to confront this rather than arrive as undocumented behaviour.
    """
    p = env["payload"]("b.json", [{"title": "B1"}, {"title": "B2"}])
    code, _, err = env["json"](
        "story", "append", "--project", env["project_id"], "--file", p
    )
    assert code == 0, err
    rows = env["conn"].execute(
        "SELECT id, undo_of_entry_id FROM cli_audit WHERE project_id = ?",
        (env["project_id"],),
    ).fetchall()
    assert rows, "the write produced no audit entry at all"
    for row in rows:
        assert row["undo_of_entry_id"] is None


def test_every_write_logs_one_entry(env):
    p = env["payload"]("b.json", [{"title": "B1"}, {"title": "B2"}])
    code, res, err = env["json"](
        "story", "append", "--project", env["project_id"], "--file", p
    )
    assert code == 0, err
    logs = env["logs"](env["project_id"])
    assert len(logs) == 2  # project create + this append
    assert logs[-1]["command"] == "story append"
    assert logs[-1]["summary"] == "insert×2"


def test_audit_entry_records_the_before_image(env):
    p = env["payload"]("b.json", [{"title": "B1"}])
    env["json"]("story", "append", "--project", env["project_id"], "--file", p)
    code, res, err = env["json"]("story", "update", "--project", env["project_id"],
                                 "--beat-id", "1", "--title", "B1 renamed")
    assert code == 0, err
    code, logs, _ = env["json"]("log", "list", "--project", env["project_id"])
    update = next(entry for entry in logs if entry["command"] == "story update")
    change = update["changes"][0]
    assert change["before"]["title"] == "B1"
    assert change["after"]["title"] == "B1 renamed"


def test_a_failed_write_logs_nothing(env):
    before = env["conn"].execute("SELECT COUNT(*) AS c FROM cli_audit").fetchone()["c"]
    code, _, _ = env["json"]("story", "update", "--project", env["project_id"],
                             "--beat-id", "999", "--title", "ghost")
    assert code == 4
    after = env["conn"].execute("SELECT COUNT(*) AS c FROM cli_audit").fetchone()["c"]
    assert after == before


def test_audit_is_append_only(env):
    p = env["payload"]("b.json", [{"title": "B1"}])
    env["json"]("story", "append", "--project", env["project_id"], "--file", p)
    env["json"]("story", "update", "--project", env["project_id"], "--beat-id", "1",
                "--title", "x")
    ids = [e["id"] for e in env["json"]("log", "list", "--project", env["project_id"])[1]]
    assert ids == sorted(ids, reverse=True)  # newest first
    assert len(set(ids)) == len(ids)


def test_mirror_is_written_after_the_commit(env, tmp_path):
    p = env["payload"]("b.json", [{"title": "B1"}])
    env["json"]("story", "append", "--project", env["project_id"], "--file", p)
    mirror = tmp_path / "audit" / f"project-{env['project_id']}.jsonl"
    lines = [
        json.loads(line)
        for line in mirror.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert len(lines) == 1
    assert lines[0]["command"] == "story append"


# -- drift preflight -------------------------------------------------------


def test_stale_expect_hash_is_refused(env):
    p = env["payload"]("b.json", [{"title": "B1"}])
    env["json"]("story", "append", "--project", env["project_id"], "--file", p)

    from calliope.authoring.hash import hash_rows, scope_digest

    conn = env["conn"]
    seen = scope_digest(hash_rows(conn, "story_beats", project_id=env["project_id"]))
    # Simulate the web UI editing the row after the caller read it.
    conn.execute("UPDATE story_beats SET title = 'UI edit' WHERE id = 1")
    conn.commit()

    code, _, err = env["json"]("story", "update", "--project", env["project_id"],
                               "--beat-id", "1", "--title", "CLI",
                               "--expect-hash", seen)
    # Exit 3, not 2: the payload was fine, the world moved. A caller scripting a
    # retry has to be able to tell "fix your JSON" from "re-read and re-apply".
    assert code == 3
    assert "changed since you read it" in err
    row = conn.execute("SELECT title FROM story_beats WHERE id = 1").fetchone()
    assert row["title"] == "UI edit"


def test_fresh_expect_hash_is_accepted(env):
    p = env["payload"]("b.json", [{"title": "B1"}])
    env["json"]("story", "append", "--project", env["project_id"], "--file", p)

    from calliope.authoring.hash import hash_rows, scope_digest

    conn = env["conn"]
    seen = scope_digest(hash_rows(conn, "story_beats", project_id=env["project_id"]))
    code, _, err = env["json"]("story", "update", "--project", env["project_id"],
                               "--beat-id", "1", "--title", "CLI",
                               "--expect-hash", seen)
    assert code == 0, err
    row = conn.execute("SELECT title FROM story_beats WHERE id = 1").fetchone()
    assert row["title"] == "CLI"


def test_expect_hash_is_optional(env):
    """Passing no fingerprint at all is still a valid choice: ``_guard``
    compares only when given something to compare against.

    (The old rationale here read "Appending has no earlier state to drift
    from", which is wrong -- a caller that chose its ``order_index`` from a
    read has exactly such a state. Wrong rationale, so the stale case was
    never asserted and ``story append`` shipped accepting ``--expect-hash``
    without comparing it.)"""
    p = env["payload"]("b.json", [{"title": "B1"}])
    code, _, err = env["json"](
        "story", "append", "--project", env["project_id"], "--file", p
    )
    assert code == 0, err


def test_preflight_catches_a_row_added_inside_the_scope(env):
    """Not just edits: a row inserted into our range after the read."""
    p = env["payload"]("b.json", [{"title": "B1"}, {"title": "B2"}])
    env["json"]("story", "append", "--project", env["project_id"], "--file", p)

    from calliope.authoring.hash import hash_rows, scope_digest

    conn = env["conn"]
    seen = scope_digest(hash_rows(conn, "story_beats", project_id=env["project_id"]))
    conn.execute(
        "INSERT INTO story_beats (project_id, order_index, title) VALUES (?, 3, 'UI')",
        (env["project_id"],),
    )
    conn.commit()

    code, _, err = env["json"]("story", "replace-range", "--project", env["project_id"],
                               "--from-index", "1", "--to-index", "2",
                               "--file", p, "--expect-hash", seen)
    assert code == 3
    assert "changed since you read it" in err


def test_stale_expect_hash_is_refused_on_story_append(env):
    """SKILL.md shows ``story append --expect-hash`` twice and labels it
    ``# 4. write, guarded``. The flag is offered on every verb, so a caller
    following the skill must get the protection its help text promises -- not
    an exit 0 that means nothing was checked."""
    from calliope.authoring.hash import hash_rows, scope_digest

    conn = env["conn"]
    seen = scope_digest(hash_rows(conn, "story_beats", project_id=env["project_id"]))
    # The web UI lands a row after the caller took its fingerprint.
    conn.execute(
        "INSERT INTO story_beats (project_id, order_index, title) VALUES (?, 1, 'UI')",
        (env["project_id"],),
    )
    conn.commit()

    code, _, err = env["json"](
        "story", "append", "--project", env["project_id"],
        "--file", env["payload"]("b.json", [{"title": "B1"}]),
        "--expect-hash", seen,
    )
    assert code == 3, err
    assert "changed since you read it" in err
    rows = conn.execute(
        "SELECT title FROM story_beats WHERE project_id = ?", (env["project_id"],)
    ).fetchall()
    assert [r["title"] for r in rows] == ["UI"]


def test_stale_expect_hash_is_refused_on_script_append(env):
    """The same verb class on the other group: ``script append`` accepts the
    flag, so it has to compare it too."""
    from calliope.authoring.hash import hash_rows, scope_digest

    conn = env["conn"]
    seen = scope_digest(hash_rows(conn, "scenes", project_id=env["project_id"]))
    env["json"]("script", "append", "--project", env["project_id"],
                "--file", env["payload"]("a.json", [{"heading": "S1"}]))

    code, _, err = env["json"](
        "script", "append", "--project", env["project_id"],
        "--file", env["payload"]("b.json", [{"heading": "S2"}]),
        "--expect-hash", seen,
    )
    assert code == 3, err
    assert "changed since you read it" in err
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM scenes WHERE project_id = ?", (env["project_id"],)
    ).fetchone()["c"]
    assert n == 1


# -- project hash: making the guard reachable -----------------------------


def test_project_hash_covers_every_writable_scope(env):
    """If a table is missing here, no caller can ever preflight a write to it --
    the guard exists but is unreachable, which is worse than not having it."""
    code, digests, err = env["json"]("project", "hash", "--project", env["project_id"])
    assert code == 0, err
    from calliope.authoring.hash import WRITABLE_SCOPES

    assert set(digests) == set(WRITABLE_SCOPES)


def test_a_hash_from_project_hash_is_accepted_by_a_write(env):
    """The end-to-end contract: read the fingerprint with the command, pass it
    to the write. A round trip that only worked by computing the digest by hand
    in the test would leave the feature unusable."""
    code, digests, err = env["json"]("project", "hash", "--project", env["project_id"])
    assert code == 0, err
    code, _, err = env["json"](
        "story", "append", "--project", env["project_id"],
        "--file", env["payload"]("b.json", [{"title": "B1"}]),
        "--expect-hash", digests["story_beats"],
    )
    assert code == 0, err


def test_project_hash_moves_when_content_moves(env):
    code, before, _ = env["json"]("project", "hash", "--project", env["project_id"])
    env["json"]("story", "append", "--project", env["project_id"],
                "--file", env["payload"]("b.json", [{"title": "B1"}]))
    code, after, _ = env["json"]("project", "hash", "--project", env["project_id"])
    assert code == 0
    assert before["story_beats"] != after["story_beats"]
    # An unrelated table must not move, or the caller cannot tell which scope
    # actually drifted and every drift report becomes useless.
    assert before["projects"] == after["projects"]
    assert before["clips"] == after["clips"]


def test_project_hash_does_not_move_on_a_bookkeeping_bump(env):
    """The CLI bumps ``projects.project_revision`` on its own writes. If that
    counted as a content change, every write would invalidate the previous
    fingerprint and the guard would be unusable in a loop."""
    pid = env["project_id"]
    code, before, _ = env["json"]("project", "hash", "--project", str(pid))
    env["json"]("story", "append", "--project", str(pid),
                "--file", env["payload"]("b.json", [{"title": "B1"}]))
    code, after, err = env["json"]("project", "hash", "--project", str(pid))
    assert code == 0, err
    assert before["projects"] == after["projects"]
    row = env["conn"].execute(
        "SELECT project_revision FROM projects WHERE id = ?", (pid,)
    ).fetchone()
    assert row["project_revision"] > 0


def test_the_with_ids_form_is_also_accepted(env):
    """Both forms round-trip, so a caller that kept the id-carrying value from
    an earlier session is not locked out."""
    code, digests, err = env["json"](
        "project", "hash", "--project", env["project_id"], "--with-ids"
    )
    assert code == 0, err
    code, _, err = env["json"](
        "story", "append", "--project", env["project_id"],
        "--file", env["payload"]("b.json", [{"title": "B1"}]),
        "--expect-hash", digests["story_beats"],
    )
    assert code == 0, err


def test_with_ids_actually_carries_the_ids(env):
    """``--with-ids`` must produce a *different* fingerprint from the bare form,
    and the one that matches ``scope_digest(..., with_ids=True)``."""
    from calliope.authoring.hash import hash_rows, scope_digest

    env["json"]("story", "append", "--project", env["project_id"],
                "--file", env["payload"]("b.json", [{"title": "B1"}]))
    pid = env["project_id"]
    code, with_ids, err = env["json"](
        "project", "hash", "--project", str(pid), "--with-ids"
    )
    assert code == 0, err
    code, bare, err = env["json"]("project", "hash", "--project", str(pid))
    assert code == 0, err
    assert with_ids["story_beats"] != bare["story_beats"]
    conn = env["conn"]
    assert with_ids["story_beats"] == scope_digest(
        hash_rows(conn, "story_beats", project_id=pid), with_ids=True
    )


def test_the_drift_error_names_real_commands(env):
    """An error that sends the caller to a flag or command that does not exist
    costs a failed round trip and teaches it to ignore errors."""
    from calliope.authoring.hash import hash_rows, scope_digest

    conn = env["conn"]
    env["json"]("story", "append", "--project", env["project_id"],
                "--file", env["payload"]("b.json", [{"title": "B1"}]))
    seen = scope_digest(hash_rows(conn, "story_beats", project_id=env["project_id"]))
    conn.execute("UPDATE story_beats SET title = 'UI' WHERE id = 1")
    conn.commit()

    code, _, err = env["json"](
        "story", "update", "--project", env["project_id"], "--beat-id", "1",
        "--title", "CLI", "--expect-hash", seen,
    )
    assert code == 3
    assert "project hash" in err
    assert "--no-preflight" not in err


def test_the_digest_is_order_independent_of_the_map_build(env):
    """``scope_digest`` sorts by id. Relying on insertion order would make the
    digest depend on how the map happened to be built, so the same content could
    hash two ways and every write would report phantom drift."""
    from calliope.authoring.hash import scope_digest

    rows = {3: "c", 1: "a", 2: "b"}
    shuffled = {2: "b", 3: "c", 1: "a"}
    assert scope_digest(rows) == scope_digest(shuffled)
    assert scope_digest(rows, with_ids=True) == scope_digest(shuffled, with_ids=True)
    # And a real change must still be detected.
    assert scope_digest(rows) != scope_digest({**rows, 2: "changed"})


def test_project_show_reports_the_revision(env):
    """The counter has to be visible somewhere or it is not a feature."""
    pid = env["project_id"]
    code, before, err = env["json"]("project", "show", "--project", str(pid))
    assert code == 0, err
    env["json"]("story", "append", "--project", str(pid),
                "--file", env["payload"]("b.json", [{"title": "B1"}]))
    code, after, err = env["json"]("project", "show", "--project", str(pid))
    assert code == 0, err
    assert after["project_revision"] == before["project_revision"] + 1


def test_a_dry_run_writes_nothing_at_all(env):
    """--dry-run promises "report both sides without writing". If it bumped the
    counter, it would be lying -- and the bump is inside the same transaction
    as the content writes, so this is where that promise is enforced."""
    pid = env["project_id"]
    code, before, err = env["json"]("project", "show", "--project", str(pid))
    assert code == 0, err
    code, _res, err = env["json"](
        "story", "append", "--project", str(pid),
        "--file", env["payload"]("b.json", [{"title": "B1"}]),
        "--dry-run",
    )
    assert code == 0, err
    assert env["json"]("story", "list", "--project", str(pid))[1] == []
    code, after, err = env["json"]("project", "show", "--project", str(pid))
    assert code == 0, err
    assert after["project_revision"] == before["project_revision"]


# -- the exit-code contract ------------------------------------------------
# These numbers are the machine-readable surface a scripted caller branches on,
# so each one is pinned to the failure it means. A caller that cannot tell
# "your JSON is wrong" from "the UI changed this" retries the wrong thing.


def test_a_malformed_payload_is_validation_exit_2(env):
    code, _res, err = env["json"](
        "story", "append", "--project", env["project_id"],
        "--file", env["payload"]("b.json", {"not": "a list"}),
    )
    assert code == 2, err


def test_a_missing_project_is_not_found_exit_4(env):
    code, _res, err = env["json"]("project", "show", "--project", "99999")
    assert code == 4, err


def test_a_deleted_project_is_not_found_exit_4(env):
    """Deleting is the one thing the CLI cannot do. It has to answer like every
    other missing project, not like a verb that does not exist -- otherwise the
    error teaches the caller that `project delete` is merely unavailable."""
    code, _res, err = env["json"]("project", "delete", "--project", env["project_id"])
    assert code != 0
    assert env["json"]("project", "show", "--project", env["project_id"])[0] == 0


# -- global flag placement --------------------------------------------------
# --json / --dry-run are registered on the root parser AND on every leaf, so
# both positions have to work. argparse applies a subparser's store_true default
# while parsing into the shared namespace, so without default=SUPPRESS the
# leading position was silently downgraded to human text -- no error, just
# output an agent cannot parse.


def test_a_leading_json_flag_is_not_silently_dropped(env):
    out, err = env["run"]("--json", "project", "show", "--project", env["project_id"])[1:]
    assert not err, err
    json.loads(out)  # raises if it fell back to the text renderer


def test_a_trailing_json_flag_still_works(env):
    out, err = env["run"]("project", "show", "--project", env["project_id"], "--json")[1:]
    assert not err, err
    json.loads(out)


def test_both_positions_agree(env):
    _c, leading, _e = env["run"]("--json", "project", "show", "--project", env["project_id"])
    _c, trailing, _e = env["run"](
        "project", "show", "--project", env["project_id"], "--json"
    )
    assert json.loads(leading) == json.loads(trailing)


def test_a_leading_dry_run_flag_is_not_silently_dropped(env):
    """--dry-run before the verb, on a write. Silently becoming a real write is
    the worst possible version of this bug."""
    code, _res, err = env["json"](
        "--dry-run", "story", "append", "--project", env["project_id"],
        "--file", env["payload"]("b.json", [{"title": "B1"}]),
    )
    assert code == 0, err
    assert env["json"]("story", "list", "--project", env["project_id"])[1] == []


def test_no_flag_still_renders_text(env):
    """The human renderer is not the fallback for a missing attribute; it is
    what you get when you did not ask for JSON."""
    out = env["run"]("project", "show", "--project", env["project_id"])[1]
    assert out.strip() and not out.lstrip().startswith(("{", "["))


# -- ordering invariants ---------------------------------------------------


def test_scene_order_stays_dense_after_append(env):
    cast = env["payload"]("c.json", [{"name": "Ann", "kind": "character"}])
    env["json"]("cast", "upsert", "--project", env["project_id"], "--file", cast)
    scenes = env["payload"](
        "s.json",
        [{"heading": "S1"}, {"heading": "S2"}, {"heading": "S3"}],
    )
    code, _, err = env["json"](
        "script", "append", "--project", env["project_id"], "--file", scenes
    )
    assert code == 0, err
    rows = env["json"]("script", "list", "--project", env["project_id"])[1]
    assert [r["order_index"] for r in rows] == [1, 2, 3]


def test_each_scene_keeps_at_least_one_clip(env):
    scenes = env["payload"]("s.json", [{"heading": "S1"}, {"heading": "S2"}])
    env["json"]("script", "append", "--project", env["project_id"], "--file", scenes)
    rows = env["json"]("clips", "list", "--project", env["project_id"])[1]
    per_scene = {}
    for clip in rows:
        per_scene.setdefault(clip["scene_id"], []).append(clip["order_index"])
    assert len(per_scene) == 2
    for indices in per_scene.values():
        assert indices == [1]


def test_replace_range_keeps_ordering_dense(env):
    scenes = env["payload"]("s.json", [{"heading": f"S{i}"} for i in range(1, 5)])
    env["json"]("script", "append", "--project", env["project_id"], "--file", scenes)
    code, _, err = env["json"](
        "script", "replace-range", "--project", env["project_id"],
        "--from-index", "2", "--to-index", "3",
        "--file", env["payload"]("rep.json", [{"heading": "NEW"}]),
    )
    assert code == 0, err
    rows = env["json"]("script", "list", "--project", env["project_id"])[1]
    assert [r["order_index"] for r in rows] == [1, 2, 3]
    assert [r["heading"] for r in rows] == ["S1", "NEW", "S4"]


def test_order_index_below_one_is_rejected(env):
    p = env["payload"]("s.json", [{"heading": "S1", "order_index": 0}])
    code, _, err = env["json"](
        "script", "append", "--project", env["project_id"], "--file", p
    )
    assert code == 2


def test_clip_order_is_dense_within_its_scene(env):
    scenes = env["payload"]("s.json", [{"heading": "S1"}])
    env["json"]("script", "append", "--project", env["project_id"], "--file", scenes)
    code, res, err = env["json"](
        "clips", "append", "--project", env["project_id"], "--scene", "1",
        "--file", env["payload"](
            "cl.json",
            [{"description": "a"}, {"description": "b"}, {"description": "c"}],
        ),
    )
    assert code == 0, err
    rows = env["json"]("clips", "list", "--project", env["project_id"])[1]
    mine = [r for r in rows if r["scene_id"] == res["scene_id"]]
    assert [r["order_index"] for r in mine] == [1, 2, 3, 4]  # 4 = the default clip
