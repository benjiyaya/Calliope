"""``[CARRY]``: where a per-clip continuity constraint lives, and that it is the
only place it can live.

``context set`` has no ``shots`` field on purpose. ``ensure_continuity_plan``
rebuilds every shot whenever ``based_on != basis_hash(board)``
(``continuity.py:492``) and ``normalize_plan`` never reads the previous value
(``continuity.py:336-347``) -- so a constraint written into the plan is erased by
the next recalculation, with no warning. The writable home is the clip's own
description, which already travels to the renderer.

The failure mode this file guards against is silent: the constraint looks
written, ``context get`` reports it back, and it evaporates at render time.
"""
from __future__ import annotations

import json

import pytest

from calliope.authoring.script import CARRY_PREFIX, carry_of, carry_lines


@pytest.fixture
def env(cli_env):
    return cli_env


def _scene_with_one_clip(env, description: str | None) -> int:
    code, res, err = env["json"](
        "script", "append", "--project", env["project_id"],
        "--file", env["payload"]("s.json", [{"heading": "S1"}]),
    )
    assert code == 0, err
    code, res, err = env["json"](
        "clips", "update", "--project", env["project_id"],
        "--clip-id", "1", *(["--description", description] if description is not None else []),
    )
    assert code == 0, err
    return int(res["clip_id"])


# -- parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "description,expected",
    [
        ("[CARRY] the coat stays red", "the coat stays red"),
        ("Wide of the hall\n[CARRY] keep the lantern lit", "keep the lantern lit"),
        ("  [CARRY]   indented", "indented"),
        # The block runs to the end of the description: a constraint is a
        # paragraph, and truncating at the first newline loses half of it.
        ("[CARRY]first line\nsecond line", "first line\nsecond line"),
        ("[CARRY] a\n\nb", "a\nb"),
        ("no carry here", ""),
        ("[carry] lowercase is not the marker", ""),
        ("[CARRYX] not the marker", ""),
        ("[CARRY]", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_carry_of_reads_the_marker(env, description, expected):
    clip_id = _scene_with_one_clip(env, description)
    conn = env["conn"]
    assert carry_of(conn, env["project_id"], clip_id) == expected


def test_two_markers_yield_the_whole_tail(env):
    """A second line is tolerated -- the renderer reads the whole description,
    and someone adding a constraint mid-script should not have to delete the
    old one first. The first marker starts the block."""
    clip_id = _scene_with_one_clip(
        env, "[CARRY] original\n[CARRY] added later"
    )
    assert carry_of(env["conn"], env["project_id"], clip_id) == "original\n[CARRY] added later"


def test_carry_of_ignores_another_projects_clip(env):
    """The project_id in the WHERE is the guard. A clip id is caller-supplied,
    so without it this would read across project boundaries."""
    code, res, err = env["json"](
        "project", "create", "--title", "Other"
    )
    assert code == 0, err
    other = res["project_id"]

    code, res, err = env["json"](
        "script", "append", "--project", other,
        "--file", env["payload"]("s2.json", [{"heading": "S1"}]),
    )
    assert code == 0, err
    env["conn"].execute("UPDATE clips SET description = ? WHERE id = 1", (CARRY_PREFIX + "x",))

    assert carry_of(env["conn"], other, 1) == "x"
    assert carry_of(env["conn"], env["project_id"], 1) == ""


def test_carry_of_is_safe_for_a_missing_clip(env):
    assert carry_of(env["conn"], env["project_id"], 9999) == ""
    assert carry_of(env["conn"], env["project_id"], None) == ""


def test_carry_lines_matches_carry_of(env):
    """The batch helper exists so the read face does not re-query per shot;
    it has to agree with the per-row one or the two answer differently."""
    clip_id = _scene_with_one_clip(env, "x\n[CARRY] keep it")
    conn = env["conn"]
    clips = [
        dict(r) for r in conn.execute("SELECT id, description FROM clips").fetchall()
    ]
    assert carry_lines(clips)[clip_id] == carry_of(conn, env["project_id"], clip_id)


def test_carry_lines_omits_clips_without_one(env):
    _scene_with_one_clip(env, "plain description")
    clips = [
        dict(r) for r in env["conn"].execute("SELECT id, description FROM clips")
    ]
    assert carry_lines(clips) == {}


# -- the read face ---------------------------------------------------------


def test_shots_list_can_report_the_carry(env):
    """``shots list --carry`` is how a caller checks what will reach the
    renderer per clip without reading every description."""
    _scene_with_one_clip(env, "[CARRY] the coat stays red")
    # A stored plan is needed for `shots` to have rows; write a minimal one.
    env["conn"].execute(
        "UPDATE projects SET continuity_json = ? WHERE id = ?",
        (json.dumps({"overview": {}, "requirements": {}, "shots": [{"clip_id": 1}], "based_on": "x"}), env["project_id"]),
    )
    code, res, err = env["json"](
        "shots", "list", "--project", env["project_id"], "--carry"
    )
    assert code == 0, err
    assert res == [{"clip_id": 1, "carry": "the coat stays red"}]


# -- the write face --------------------------------------------------------


def test_a_carry_survives_a_context_set(env):
    """The two must not collide: ``context set`` writes projects.continuity_json,
    the carry lives in clips.description. One is not allowed to clobber the
    other."""
    clip_id = _scene_with_one_clip(env, "[CARRY] the coat stays red")
    code, _, err = env["json"](
        "context", "set", "--project", env["project_id"],
        "--file", env["payload"](
            "ctx.json", {"overview": {"style": "noir"}, "requirements": {}}
        ),
    )
    assert code == 0, err
    assert carry_of(env["conn"], env["project_id"], clip_id) == "the coat stays red"


def test_context_set_cannot_write_shots(env):
    """The whole reason the carry lives in the description. Accepting a
    ``shots`` key here would be an offer the store cannot keep."""
    code, _, err = env["json"](
        "context", "set", "--project", env["project_id"],
        "--file", env["payload"](
            "ctx.json", {"overview": {}, "requirements": {}, "shots": [{"clip_id": 1}]}
        ),
    )
    assert code == 2
    assert "shots" in err


def test_context_set_preserves_existing_shots_and_based_on(env):
    """A metadata-only write must not look like the plan is gone, or the UI
    would recalculate the whole thing on the next render."""
    env["conn"].execute(
        "UPDATE projects SET continuity_json = ? WHERE id = ?",
        (
            json.dumps(
                {"overview": {}, "requirements": {}, "shots": [{"clip_id": 7}], "based_on": "abc"}
            ),
            env["project_id"],
        ),
    )
    code, res, err = env["json"](
        "context", "set", "--project", env["project_id"],
        "--file", env["payload"]("ctx.json", {"overview": {"style": "noir"}}),
    )
    assert code == 0, err
    assert res["shots_preserved"] == 1

    code, res, err = env["json"]("context", "get", "--project", env["project_id"])
    assert code == 0, err
    assert res["stored_based_on"] == "abc"
    assert res["overview"] == {"style": "noir"}


def test_context_set_takes_a_bare_object_or_a_one_element_array(env):
    """A caller that batched it should not have to unwrap by hand."""
    for payload in (
        {"overview": {"style": "noir"}},
        [{"overview": {"style": "noir"}}],
    ):
        code, _, err = env["json"](
            "context", "set", "--project", env["project_id"],
            "--file", env["payload"]("ctx.json", payload),
        )
        assert code == 0, err


def test_context_set_refuses_two_objects(env):
    """Two ledgers means guessing which wins, and a wrong guess silently
    discards half the user's continuity work."""
    code, _, err = env["json"](
        "context", "set", "--project", env["project_id"],
        "--file", env["payload"](
            "ctx.json", [{"overview": {"style": "a"}}, {"overview": {"style": "b"}}]
        ),
    )
    assert code == 2
    assert "exactly one" in err
