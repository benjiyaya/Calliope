"""A dirty third-party payload must not reach the database.

The workflow this CLI exists for is: a third-party skill converts a novel into
structured content, and the CLI writes it into Calliope. Whatever produced that
JSON -- a model, a scraper, a translation tool -- it will occasionally emit an
envelope instead of the rows, a near-miss enum, a rendered path, or a field name
from a different schema version.

Every case below is a way that could go wrong. The requirement is the same for
all of them: **reject before any SQL runs, write nothing at all, and say which
field was wrong**. The three failure modes this file exists to prevent are:

* a *silent* drop -- the field is ignored and the content quietly goes missing
* a *partial* write -- row 1 lands, row 2 fails, the script is half-updated
* a *pollution* write -- render state or a foreign key from another tool's
  schema gets stored and later breaks the render pipeline

The last one matters most: ``extra="forbid"`` on every model is what stops a
field name from a different schema version being written into a column that
merely happens to have the same name.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def env(cli_env):
    return cli_env


def _scene(env, name="s.json", row=None):
    """A scene to hang clips off, so clip payloads get past scene lookup."""
    code, _out, err = _write(
        env, "script", "append", name, [row or {"heading": "S1"}]
    )
    assert code == 0, err
    return 1


def _fenced_file(env, body: str):
    p = env["data_dir"] / "fenced.json"
    p.write_text(f"```json\n{body}\n```", encoding="utf-8")
    return p


def _clip(env, clip_id):
    """Read one clip back by id.

    ``clips list --scene`` returns every clip of the scene, and every scene
    already has the auto-generated default one -- so looking up by position
    would read a different row than the one just written.
    """
    code, clips, err = env["json"](
        "clips", "list", "--project", env["project_id"], "--scene", "1"
    )
    assert code == 0, err
    for clip in clips:
        if clip["id"] == int(clip_id):
            return clip
    raise AssertionError(f"clip {clip_id} not in {clips}")


def _counts(env, *tables):
    conn = env["conn"]
    return {
        table: conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        for table in tables
    }


def _write(env, group, verb, name, data, *extra):
    """Same shape as ``run_json`` -- (code, result, stderr) -- so a caller can
    assert on the parsed result without a second CLI invocation."""
    return env["json"](
        group, verb, "--project", env["project_id"],
        "--file", env["payload"](name, data), *extra,
    )


# -- wrong envelope --------------------------------------------------------


def test_an_llm_envelope_instead_of_rows_is_rejected(env):
    """Models like to wrap: ``{"beats": [...]}``. Guessing which key holds the
    rows is how a partial import happens."""
    code, _res, err = _write(
        env, "story", "append", "e.json",
        {"beats": [{"title": "B1"}], "notes": "ignore me"},
    )
    assert code == 2
    assert "array" in err
    assert _counts(env, "story_beats")["story_beats"] == 0


def test_a_markdown_fenced_payload_is_rejected_not_parsed(env):
    """The payload is JSON, full stop. Unwrapping ```json fences means guessing
    at prose, and a document that has been through a chat transcript is not
    reliably delimited."""
    code, _out, err = env["json"](
        "story", "append", "--project", env["project_id"],
        "--file", _fenced_file(env, '[{"title": "B1"}]'),
    )
    assert code != 0
    assert "error" in err.lower()
    assert _counts(env, "story_beats")["story_beats"] == 0


def test_a_json_string_is_not_a_payload(env):
    code, _out, _err = _write(env, "story", "append", "s.json", "just a string")
    assert code == 2


# -- near-miss enum values -------------------------------------------------


def test_a_lowercase_shot_size_is_rejected_not_normalised(env):
    """``closeup`` vs ``closeUp``. Mapping it would be a guess: if the
    whitelist ever gains a value that differs only by case, the mapping picks
    the wrong one silently."""
    _scene(env)
    code, _res, err = _write(
        env, "clips", "append", "c.json",
        [{"description": "x", "shot_size": "closeup"}], "--scene", "1",
    )
    assert code == 2
    assert "closeUp" in err


def test_a_translated_shot_size_is_rejected(env):
    _scene(env)
    code, _out, _err = _write(
        env, "clips", "append", "c.json",
        [{"description": "x", "shot_size": "特写"}], "--scene", "1",
    )
    assert code == 2


def test_a_blank_shot_size_means_absent_not_invalid(env):
    """The one normalisation that is safe: an empty form field means unset.
    Anything stricter turns a payload copied out of the web UI into an error."""
    _scene(env)
    code, _out, err = _write(
        env, "clips", "append", "c.json",
        [{"description": "x", "shot_size": ""}], "--scene", "1",
    )
    assert code == 0, err
    clip = env["json"]("clips", "list", "--project", env["project_id"],
                       "--scene", "1")[1][0]
    assert clip["shot_size"] is None


# -- render state pollution ------------------------------------------------


@pytest.mark.parametrize(
    "table,group,verb,row,field",
    [
        ("clips", "clips", "append", {"description": "x"}, "clip_path"),
        ("clips", "clips", "append", {"description": "x"}, "workflow_id"),
        ("clips", "clips", "append", {"description": "x"}, "video_settings_json"),
        ("clips", "clips", "append", {"description": "x"}, "chain_from_prev"),
        ("scenes", "script", "append", {"heading": "S1"}, "workflow_id"),
        ("story_beats", "story", "append", {"title": "B1"}, "project_id"),
        ("clips", "clips", "append", {"description": "x"}, "created_at"),
    ],
)
def test_render_and_bookkeeping_fields_cannot_be_written(env, table, group, verb, row, field):
    """These columns belong to the render pipeline and to SQLite bookkeeping.
    Accepting them would let an agent overwrite a generated video path with
    whatever the payload claimed."""
    if group == "clips":
        _scene(env)
    code, _res, err = _write(
        env, group, verb, "r.json", [{**row, field: "attacker/path.mp4"}],
        *(["--scene", "1"] if group == "clips" else []),
    )
    assert code == 2, err
    assert field in err
    # The clips table already holds the scene's default clip, so compare the
    # count before and after instead of expecting zero.
    if table == "clips":
        assert _counts(env, table)[table] == 1
    else:
        assert _counts(env, table)[table] == 0


def test_a_rendered_sheet_path_cannot_be_claimed_through_upsert(env):
    """``sheet_path`` is writable through ``cast update`` (the user points the
    UI at an existing sheet) but NOT through ``cast upsert`` -- upsert is the
    "here is a spec" path, and a spec does not carry a rendered asset."""
    code, _res, err = _write(
        env, "cast", "upsert", "c.json",
        [{"name": "Ann", "kind": "character", "sheet_path": "sheet/ann.png"}],
    )
    assert code == 2
    assert "sheet_path" in err
    assert _counts(env, "characters")["characters"] == 0


# -- reference integrity ---------------------------------------------------


def test_a_scene_naming_an_unknown_character_is_rejected(env):
    """The scene would render with a character the continuity board has never
    heard of, and the name would silently not link to anything."""
    code, _res, err = _write(
        env, "script", "append", "s.json",
        [{"heading": "S1", "characters": ["Nobody"]}],
    )
    assert code == 2
    assert "Nobody" in err
    assert _counts(env, "scenes", "scene_characters") == {
        "scenes": 0, "scene_characters": 0,
    }


def test_a_scene_pointing_at_a_missing_location_is_rejected(env):
    """``scenes.location_id`` has NO foreign key (db.py:77). Nothing in the
    schema stops a dangling id, so this is checked by hand."""
    code, _res, err = _write(
        env, "script", "append", "s.json",
        [{"heading": "S1", "location_id": 999}],
    )
    assert code == 2
    assert "999" in err
    assert _counts(env, "scenes")["scenes"] == 0


def test_a_cast_row_appearing_twice_in_one_payload_is_rejected(env):
    """Last-write-wins within a batch is a coin flip the caller cannot see."""
    code, _res, err = _write(
        env, "cast", "upsert", "c.json",
        [{"name": "Ann", "kind": "character", "role": "lead"},
         {"name": "Ann", "kind": "character", "role": "extra"}],
    )
    assert code == 2
    assert "more than once" in err
    assert _counts(env, "characters")["characters"] == 0


# -- atomicity -------------------------------------------------------------


def test_one_bad_row_rejects_the_whole_payload(env):
    """A partial write is worse than a failed one: the script ends up with nine
    new beats and no record that the tenth was refused."""
    code, _res, err = _write(
        env, "story", "append", "b.json",
        [{"title": "B1"}, {"title": "B2"}, {"shot_size": "wide"}],
    )
    assert code == 2
    assert "1 of 3" in err
    assert _counts(env, "story_beats")["story_beats"] == 0


def test_every_bad_row_is_named_not_just_the_first(env):
    code, _res, err = _write(
        env, "story", "append", "b.json",
        [{"description": "no title"}, {"title": "   "}],
    )
    assert code == 2
    # Row indices, so a 40-row payload can be fixed in one pass.
    assert "[0]" in err
    assert "[1]" in err


def test_a_rejected_payload_leaves_no_audit_entry(env):
    """The log has to mean "this happened". A rejected payload never happened."""
    before = len(env["logs"](env["project_id"]))
    _write(env, "story", "append", "b.json", [{"shot_size": "wide"}])
    assert len(env["logs"](env["project_id"])) == before


# -- type confusion --------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["[1,2]", "1,2", {"0": 1}, {"lines": [1]}, [None], ["a"], True]
)
def test_dialog_lines_covered_must_be_a_list_of_ints(env, value):
    """The column is TEXT holding a JSON array (coverage_agent.py:288). Writing
    the string ``"[1,2]"`` produces a row that reads back as one opaque string
    and the coverage agent silently finds no covered lines."""
    _scene(env)
    code, _out, _err = _write(
        env, "clips", "append", "c.json",
        [{"description": "x", "dialog_lines_covered": value}], "--scene", "1",
    )
    assert code == 2


def test_order_index_zero_is_rejected(env):
    """0 is not a position. ``scenes.order_index`` is dense 1..N and three
    subsystems slice on it."""
    code, _out, _err = _write(
        env, "story", "append", "b.json", [{"title": "B1", "order_index": 0}]
    )
    assert code == 2


def test_an_overlong_title_is_rejected(env):
    """A 500-character beat title is a sign the wrong field was filled in."""
    code, _out, _err = _write(
        env, "story", "append", "b.json", [{"title": "x" * 500}]
    )
    assert code == 2


def test_a_negative_duration_is_rejected(env):
    _scene(env)
    code, _out, _err = _write(
        env, "clips", "append", "c.json",
        [{"description": "x", "duration_sec": -3}], "--scene", "1",
    )
    assert code == 2


def test_a_one_based_dialog_line_is_accepted(env):
    """The counterpart to the rejection above, so the rule reads as "one-based
    integers" rather than "rejected"."""
    _scene(env)
    code, res, err = _write(
        env, "clips", "append", "c.json",
        [{"description": "x", "dialog_lines_covered": [1, 3]}], "--scene", "1",
    )
    assert code == 0, err
    # Read back the clip the command reported, not "the first clip of the
    # scene" -- the scene already had the auto-generated default one.
    clip = _clip(env, res["clip_ids"][0])
    assert clip["dialog_lines_covered"] == [1, 3]


def test_a_zero_dialog_line_is_rejected(env):
    """``clips.dialog_lines_covered`` is 1-based like every other index here;
    a 0 would silently offset the whole coverage map."""
    _scene(env)
    code, _out, _err = _write(
        env, "clips", "append", "c.json",
        [{"description": "x", "dialog_lines_covered": [0]}], "--scene", "1",
    )
    assert code == 2


# -- legitimate content that must NOT be rejected ---------------------------


def test_non_ascii_names_are_accepted_verbatim(env):
    """A novel in Chinese is the primary use case. Sanitising the names would
    break the script-to-cast link the moment a scene referenced them."""
    code, _out, err = _write(
        env, "cast", "upsert", "c.json",
        [{"name": "林小满", "kind": "character", "appearance": "红色外套"}],
    )
    assert code == 0, err
    rows = env["json"]("cast", "list", "--project", env["project_id"])[1]
    assert rows["characters"][0]["name"] == "林小满"


def test_a_description_with_markdown_is_stored_as_written(env):
    """Markdown in a description is the caller's business. Refusing it would
    mean the CLI had opinions about prose."""
    _scene(env)
    body = "**Wide.** Ann enters.\n- lantern lit\n- coat red"
    code, res, err = _write(
        env, "clips", "append", "c.json", [{"description": body}], "--scene", "1"
    )
    assert code == 0, err
    clip = _clip(env, res["clip_ids"][0])
    assert clip["description"] == body


def test_an_empty_optional_field_is_accepted_as_null(env):
    code, _out, err = _write(
        env, "story", "append", "b.json",
        [{"title": "B1", "description": "", "order_index": None}],
    )
    assert code == 0, err
    row = env["json"]("story", "get", "--project", env["project_id"],
                      "--beat-id", "1")[1]
    assert row["title"] == "B1"


def test_a_payload_read_from_stdin_works(env):
    """``--file -`` is how an agent pipes JSON without touching the disk."""
    import io
    import sys

    from calliope.cli.main import main

    stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps([{"title": "piped"}]))
    try:
        code = main(
            ["story", "append", "--project", str(env["project_id"]),
             "--file", "-", "--json"]
        )
    finally:
        sys.stdin = stdin
    assert code == 0
    beats = env["json"]("story", "list", "--project", env["project_id"])[1]
    assert [b["title"] for b in beats] == ["piped"]


def test_a_missing_file_is_an_error_not_a_crash(env):
    code, _out, err = env["json"](
        "story", "append", "--project", env["project_id"],
        "--file", env["data_dir"] / "nope.json",
    )
    assert code != 0
    assert "error" in err.lower()


def test_malformed_json_is_an_error_not_a_crash(env):
    p = env["data_dir"] / "bad.json"
    p.write_text("{not json at all", encoding="utf-8")
    code, _out, err = env["json"](
        "story", "append", "--project", env["project_id"], "--file", p
    )
    assert code != 0
    assert "error" in err.lower()
