"""``plan next``: every branch, and the properties that must hold across all of
them.

The rule this encodes is the one the opencode skill repeats most -- write beats
before scenes, scenes before clips, context after the script it describes. If it
lives only in the skill's prose it rots the moment someone adds a group, so it
lives in ``cli/plan.py`` and is pinned here.

This is the only decision function that reads the whole project, so it is also
the easiest place to write a bug that quietly corrupts a script rather than
failing. Hence the invariants at the bottom: whatever it says, the answer is
actionable, self-explaining, and never calls the model.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from calliope.db import get_db, migrate_db
from calliope.cli.plan import GATING_TABLES, next_step


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A project whose content grows on demand, one step at a time."""
    import calliope.config as config_module

    monkeypatch.setattr(config_module.settings, "data_dir", tmp_path)
    monkeypatch.setattr(config_module.settings, "assets_dir", tmp_path / "assets")
    monkeypatch.setattr(config_module.Settings, "save_config_file", lambda self: None)
    asyncio.run(migrate_db(config_module.settings.db_path))

    conn = get_db(config_module.settings.db_path)
    conn.isolation_level = None
    pid = int(
        conn.execute("INSERT INTO projects (title) VALUES ('W')").lastrowid
    )

    class World:
        def beats(self, n=1):
            conn.executemany(
                "INSERT INTO story_beats (project_id, order_index, title) VALUES (?, ?, ?)",
                [(pid, i + 1, f"B{i + 1}") for i in range(n)],
            )
            return self

        def cast(self, n=1):
            conn.executemany(
                "INSERT INTO characters (project_id, name) VALUES (?, ?)",
                [(pid, f"C{i + 1}") for i in range(n)],
            )
            return self

        def scenes(self, n=1):
            for i in range(n):
                sid = int(
                    conn.execute(
                        "INSERT INTO scenes (project_id, order_index, heading) "
                        "VALUES (?, ?, ?)",
                        (pid, i + 1, f"S{i + 1}"),
                    ).lastrowid
                )
                conn.execute(
                    "INSERT INTO clips (project_id, scene_id, order_index) "
                    "VALUES (?, ?, 1)",
                    (pid, sid),
                )
            return self

        def shot_list(self, scene_order=1, description="a wide of the hall"):
            conn.execute(
                "UPDATE clips SET description = ? WHERE scene_id = "
                "(SELECT id FROM scenes WHERE project_id = ? AND order_index = ?)",
                (description, pid, scene_order),
            )
            return self

        def ledger(self, based_on):
            """Store a plan as the UI would, at a chosen based_on."""
            conn.execute(
                "UPDATE projects SET continuity_json = ? WHERE id = ?",
                (
                    json.dumps(
                        {
                            "overview": {"style": "noir"},
                            "requirements": {"lighting": "low key"},
                            "shots": [{"clip_id": 1}],
                            "based_on": based_on,
                        }
                    ),
                    pid,
                ),
            )
            return self

        def write_json(self, blob):
            """An arbitrary continuity_json, for the malformed-blob cases."""
            conn.execute(
                "UPDATE projects SET continuity_json = ? WHERE id = ?", (blob, pid)
            )
            return self

    try:
        yield conn, pid, World()
    finally:
        conn.close()


# -- one test per branch ---------------------------------------------------


def test_empty_project_wants_beats(world):
    _conn, pid, _w = world
    out = next_step(_conn, pid)
    assert out["step"] == "story.append"
    assert "beat list" in out["why"]


def test_beats_but_no_cast_wants_cast(world):
    conn, pid, w = world
    w.beats(3)
    out = next_step(conn, pid)
    assert out["step"] == "cast.upsert"
    assert out["counts"]["story_beats"] == 3


def test_cast_but_no_script_wants_scenes(world):
    conn, pid, w = world
    w.beats().cast()
    assert next_step(conn, pid)["step"] == "script.append"


def test_default_clips_do_not_count_as_a_shot_list(world):
    """A scene always has a clip (ensure_default_clip), so existence is not
    the test -- a description is. Missing this is the single most likely way
    `plan next` reports 'done' on an unshot script."""
    conn, pid, w = world
    w.beats().cast().scenes(2)
    out = next_step(conn, pid)
    assert out["step"] == "clips.append"
    assert len(out["targets"]) == 2


def test_blank_description_is_still_undescribed(world):
    """``'   '`` is not a description. TRIM in the query, not in Python, so a
    whitespace-only description cannot pass as shot-listed."""
    conn, pid, w = world
    w.beats().cast().scenes(1).shot_list(1, "   ")
    assert next_step(conn, pid)["step"] == "clips.append"


def test_one_unshot_scene_out_of_three_is_reported_alone(world):
    conn, pid, w = world
    w.beats().cast().scenes(3).shot_list(1).shot_list(2)
    out = next_step(conn, pid)
    assert out["step"] == "clips.append"
    assert len(out["targets"]) == 1


def test_fully_shot_without_a_ledger_wants_context(world):
    conn, pid, w = world
    w.beats().cast().scenes(1).shot_list()
    out = next_step(conn, pid)
    assert out["step"] == "context.set"
    assert out["plan_state"] == "missing"


def test_a_stale_derived_plan_still_says_render(world):
    """Editing the board stales the *derived* shots, not the writable ledger.

    ``based_on`` records what ``ensure_continuity_plan`` computed the per-shot
    breakdown from, and only that function can refresh it -- it needs the model,
    which the CLI never calls. So a board edit cannot send ``plan next`` back to
    ``context.set``: there is no ledger change the caller could make that would
    clear the flag, and asking again would loop forever.

    What it must do instead is *say* the plan is stale, so the caller knows the
    web UI will recalculate it on render.
    """
    from calliope.authoring.context import plan_state

    conn, pid, w = world
    w.beats().cast().scenes(1).shot_list()
    basis = plan_state(conn, pid)["basis_hash"]
    w.ledger(basis)
    assert next_step(conn, pid)["step"] == "render"

    # Edit a clip's description. That is inside the board, so based_on stops
    # matching and the next render would recalculate the plan.
    conn.execute("UPDATE clips SET description = 'a close on her hand'")
    out = next_step(conn, pid)
    assert out["step"] == "render"
    assert out["plan_state"] == "stale"
    assert basis in out["note"]
    assert "no CLI action" in out["note"]
    # `authored` is the gate, and the ledger still has content in it.
    assert plan_state(conn, pid)["authored"] is True


def test_clearing_the_ledger_sends_plan_next_back_to_context(world):
    """The flip side: with nothing authored, ``context.set`` comes back.

    ``authored`` is what makes the terminal step reachable in the first place,
    so removing the content has to un-gate it.
    """
    from calliope.authoring.context import plan_state

    conn, pid, w = world
    w.beats().cast().scenes(1).shot_list()
    w.ledger(plan_state(conn, pid)["basis_hash"])
    assert next_step(conn, pid)["step"] == "render"

    conn.execute("UPDATE projects SET continuity_json = NULL WHERE id = ?", (pid,))
    state = plan_state(conn, pid)
    assert state["authored"] is False
    assert next_step(conn, pid)["step"] == "context.set"


def test_editing_the_beats_does_not_stale_the_ledger(world):
    """``basis_hash`` covers clips, characters, and locations -- not
    ``story_beats`` (continuity.py:279-281). The ledger describes shots, so a
    beat edit cannot invalidate it. Worth pinning: it looks like an oversight
    from here and is not."""
    from calliope.authoring.context import plan_state

    conn, pid, w = world
    w.beats().cast().scenes(1).shot_list()
    basis = plan_state(conn, pid)["basis_hash"]
    w.ledger(basis)

    w.beats(2)
    conn.execute("UPDATE story_beats SET title = 'rewritten'")
    assert next_step(conn, pid)["step"] == "render"


def test_everything_agreeing_says_render(world):
    from calliope.authoring.context import plan_state

    conn, pid, w = world
    w.beats().cast().scenes(1).shot_list()
    w.ledger(plan_state(conn, pid)["basis_hash"])
    out = next_step(conn, pid)
    assert out["step"] == "render"
    assert out["plan_state"] == "current"
    # No hint: there is no CLI command for the next step, by design.
    assert "hint" not in out


def test_unknown_project_is_not_found(world):
    from calliope.authoring.service import NotFound

    conn, _pid, _w = world
    with pytest.raises(NotFound):
        next_step(conn, 9999)


# -- a malformed stored plan counts as missing ----------------------------


@pytest.mark.parametrize(
    "blob",
    [
        None,
        "",
        "   ",
        "{not json",
        '{"overview": {}}',            # no shots key
        '{"shots": {}}',               # shots is not a list
        '"a string"',                 # not an object
    ],
    ids=["null", "empty", "blank", "bad-json", "no-shots", "shots-not-list", "not-object"],
)
def test_a_half_written_plan_reads_as_missing(world, blob):
    """Mirrors ``_parse_stored`` (continuity.py:370-379). If the UI would treat
    this blob as absent, so must we -- otherwise `plan next` claims the ledger
    exists and the UI silently drops it on the next recalculation."""
    from calliope.authoring.context import plan_state

    conn, pid, w = world
    w.beats().cast().scenes(1).shot_list()
    w.write_json(blob)
    assert plan_state(conn, pid)["state"] == "missing"
    assert next_step(conn, pid)["step"] == "context.set"


# -- properties every answer must satisfy ---------------------------------


def test_counts_cover_every_gating_table(world):
    conn, pid, _w = world
    assert set(next_step(conn, pid)["counts"]) == set(GATING_TABLES)


def test_every_answer_explains_itself(world):
    """`why` is what an agent relays to the user. An empty one means the skill
    has to invent an explanation, which is where wrong advice comes from."""
    conn, pid, w = world
    for _ in range(5):
        out = next_step(conn, pid)
        assert out["why"].strip()
        assert out["step"] in {
            "story.append", "cast.upsert", "script.append",
            "clips.append", "context.set", "render",
        }
        if out["step"] == "story.append":
            w.beats()
        elif out["step"] == "cast.upsert":
            w.cast()
        elif out["step"] == "script.append":
            w.scenes()
        elif out["step"] == "clips.append":
            w.shot_list()
        elif out["step"] == "context.set":
            break


def test_repeated_calls_are_stable(world):
    """`plan next` is a read. Calling it twice must not change the answer --
    if it did, the skill would loop."""
    conn, pid, w = world
    w.beats().cast().scenes(2).shot_list(1)
    assert next_step(conn, pid) == next_step(conn, pid)


def test_the_hint_always_names_a_real_command(world):
    """A hint that points at a command that does not exist costs the caller a
    failed shell round trip and teaches it to ignore hints."""
    from calliope.cli.main import build_parser

    parser = build_parser()
    groups = parser._subparsers._group_actions[0].choices

    conn, pid, w = world
    for _ in range(6):
        out = next_step(conn, pid)
        hint = out.get("hint")
        if hint:
            argv = hint.split()[1:]
            assert argv[0] in groups, hint
            assert argv[1] in groups[argv[0]]._subparsers._group_actions[0].choices, hint
        if out["step"] == "story.append":
            w.beats()
        elif out["step"] == "cast.upsert":
            w.cast()
        elif out["step"] == "script.append":
            w.scenes()
        elif out["step"] == "clips.append":
            w.shot_list()
        else:
            break


def test_targets_are_scene_ids_not_positions(world):
    """`clips append --scene` accepts either, but a caller copying `targets`
    straight into `--scene` must not have to know which one this is."""
    conn, pid, w = world
    w.beats().cast().scenes(2)
    out = next_step(conn, pid)
    for scene_id in out["targets"]:
        row = conn.execute(
            "SELECT order_index FROM scenes WHERE id = ?", (scene_id,)
        ).fetchone()
        assert row is not None
