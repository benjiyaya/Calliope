"""The long-``idea`` guard: a built-in generator refuses a novel, and refuses
it *before* spending anything.

The whole cost of this feature is one real model call, so a guard that raises
after the call has been made is not a guard. Every test here therefore counts
calls, not just status codes.

Two properties are worth stating plainly, because both were discovered by
reading the code rather than by guessing:

* ``expand_scene_coverage`` is **not** guarded. It never reads ``idea`` -- its
  prompt is built from scenes, characters, and neighbors -- so guarding it
  would refuse a legitimate operation in exchange for nothing. The plan
  document listed it; the code disagrees, and the code wins.
* ``ensure_continuity_plan`` is guarded by *skipping the model*, not by
  raising. It is called from the render loop, and rendering is the user's own
  next step after the CLI authors a project. Refusing there would break the
  exact workflow this guard exists to enable.

Together those two account for the gap between "4 entry points" in the plan
and the 3 that are wired here.
"""
from __future__ import annotations

import asyncio

import pytest

from calliope.agent.idea_guard import (
    LONG_IDEA_CHARS,
    LongSourceText,
    is_long,
    refuse_long_idea,
    skip_reason,
)

NOVEL = "第一章。\n" + "他推开门，看见满地的灰。\n" * 900


def _project(**over):
    base = {"id": 7, "title": "T", "idea": "a keeper finds a bottle"}
    base.update(over)
    return base


# -- the threshold itself --------------------------------------------------


def test_the_boundary_is_the_repositorys_own_number():
    """Not a number invented here. ``harness/plugins/workspace.py`` already
    warns at this threshold, and two disagreeing numbers would mean the warning
    and the refusal disagree about what counts as a novel."""
    assert LONG_IDEA_CHARS == 2000


def test_exactly_at_the_threshold_is_still_a_logline():
    assert not is_long("x" * LONG_IDEA_CHARS)
    assert is_long("x" * (LONG_IDEA_CHARS + 1))


@pytest.mark.parametrize("value", [None, 0, "", 12345, b"bytes", [], {}])
def test_non_text_is_never_long(value):
    """Total on purpose: a caller that forgot to coerce must not trip the
    guard by accident, or a type bug turns into a 422."""
    assert not is_long(value)


# -- refusal ---------------------------------------------------------------


def test_a_novel_is_refused():
    with pytest.raises(LongSourceText) as ei:
        refuse_long_idea(_project(idea=NOVEL), generator="storyline generator")
    assert "source text" in str(ei.value)


def test_the_message_names_the_way_out():
    """"This is not allowed" is a dead end. The whole reason for the guard is
    that there IS a supported path, so the refusal has to say what it is."""
    with pytest.raises(LongSourceText) as ei:
        refuse_long_idea(_project(idea=NOVEL), generator="script generator")
    msg = str(ei.value)
    assert "calliope-cli" in msg
    assert "script generator" in msg


def test_a_logline_passes_through():
    assert refuse_long_idea(_project(), generator="script generator") is None


def test_a_missing_idea_passes_through():
    assert refuse_long_idea(_project(idea=None), generator="storyline") is None


def test_the_exception_is_a_value_error_so_existing_routers_map_it():
    """``routers/scenes.py`` already turns ValueError into 422 and keys the
    404 branch on the substring "not found". Subclassing ValueError gets the
    right status for free -- and the message must never contain that
    substring, or the guard would report a project that exists as missing."""
    assert issubclass(LongSourceText, ValueError)
    with pytest.raises(LongSourceText) as ei:
        refuse_long_idea(_project(id=7, idea=NOVEL), generator="g")
    assert "not found" not in str(ei.value).lower()


# -- the skip path ---------------------------------------------------------


def test_skip_reason_is_none_for_a_logline():
    assert skip_reason({"project": {"idea": "a keeper finds a bottle"}}) is None


def test_skip_reason_explains_itself_for_a_novel():
    reason = skip_reason({"project": {"idea": NOVEL}})
    assert reason and "source text" in reason


def test_skip_reason_survives_a_board_without_a_project():
    """``load_board`` always has one, but a guard that raises on a shape it was
    handed would turn a save into a crash."""
    assert skip_reason({}) is None
    assert skip_reason({"project": {}}) is None


# -- wired into generate_story: the real endpoint, counting model calls -----


@pytest.fixture
def no_llm(monkeypatch):
    """Record every attempt to call the model. Must stay empty."""
    calls: list[dict] = []

    async def fake(messages, temperature=0.7):
        calls.append({"messages": messages})
        raise AssertionError("the guard let a model call through")

    monkeypatch.setattr("calliope.routers.story.generate_structured", fake)
    return calls


def test_generate_story_refuses_a_novel_before_any_call(client, no_llm):
    r = client.post("/api/projects", json={"title": "Novel", "idea": NOVEL})
    pid = r.json()["id"]
    resp = client.post(f"/api/projects/{pid}/generate-story")
    assert resp.status_code == 422, resp.text
    assert no_llm == [], "a model call was made despite the guard"


def test_the_refusal_reaches_the_caller_as_422_not_500(client, no_llm):
    """A refusal is a client error with an explanation, not a server crash. The
    router's catch-all would turn it into a 500 and log a traceback for
    something nobody has to debug."""
    r = client.post("/api/projects", json={"title": "Novel", "idea": NOVEL})
    pid = r.json()["id"]
    resp = client.post(f"/api/projects/{pid}/generate-story")
    assert resp.status_code == 422
    assert "calliope-cli" in resp.json()["detail"]


def test_generate_story_still_works_on_a_logline(client, monkeypatch):
    """The guard must not break the ordinary path -- that is the 99% case, and a
    false positive here would look like "Calliope stopped generating"."""
    import calliope.routers.story as story_mod

    seen: list[int] = []

    async def ok(messages, temperature=0.7):
        seen.append(1)
        # The endpoint 502s unless the beat count matches what the target
        # duration asks for, so the stub has to honour that or this test fails
        # for a reason unrelated to the guard.
        n = 4 if "exactly 4 beats" in messages[1]["content"] else 1
        return {
            "title": "T",
            "logline": "a line",
            "beats": [
                {"order_index": i, "title": f"B{i}", "description": "d"}
                for i in range(1, n + 1)
            ],
        }

    monkeypatch.setattr(story_mod, "generate_structured", ok)
    r = client.post("/api/projects", json={"title": "Short", "idea": "tiny"})
    pid = r.json()["id"]
    assert client.post(f"/api/projects/{pid}/generate-story").status_code == 200
    assert seen, "a logline project stopped reaching the model"


def test_a_refused_project_keeps_whatever_it_had(client, no_llm):
    """``replace=True`` deletes story_beats/cast on the first chunk. A refusal
    that happened after that would destroy a CLI-authored board."""
    r = client.post("/api/projects", json={"title": "Novel", "idea": NOVEL})
    pid = r.json()["id"]
    assert client.post(f"/api/projects/{pid}/generate-story").status_code == 422
    story = client.get(f"/api/projects/{pid}/story").json()
    assert story["beats"] == []
    assert story["characters"] == []


# -- wired into generate_script --------------------------------------------


def test_generate_script_refuses_a_novel_before_any_call(client, monkeypatch):
    calls: list[dict] = []

    async def fake(messages, temperature=0.7, **kw):
        calls.append(messages)
        raise AssertionError("the guard let a model call through")

    monkeypatch.setattr("calliope.agent.script_agent.generate_structured", fake)
    r = client.post("/api/projects", json={"title": "Novel", "idea": NOVEL})
    pid = r.json()["id"]
    resp = client.post(f"/api/projects/{pid}/generate-script")
    assert resp.status_code == 422, resp.text
    assert calls == []
    assert "calliope-cli" in resp.json()["detail"]


def test_generate_script_still_works_on_a_logline(client):
    r = client.post("/api/projects", json={"title": "Short", "idea": "tiny"})
    pid = r.json()["id"]
    resp = client.post(f"/api/projects/{pid}/generate-script")
    assert resp.status_code != 422, resp.text


# -- expand_scene_coverage: deliberately NOT guarded -----------------------


def test_expand_clips_still_works_on_a_novel_project(client, monkeypatch):
    """It never reads ``idea``, so there is nothing to protect against.

    This is the test that fails if someone later "helpfully" adds the guard to
    all four entry points the plan listed -- which would block a working
    operation and cost nothing in exchange.

    The scene is inserted directly rather than generated, because on a novel
    project that is exactly how it arrives: the CLI authors the board, the user
    then refines shots in the UI. Generating one would hit the script guard --
    correctly, since the novel must not go to the model -- and the test would
    pass for the wrong reason.
    """
    import calliope.agent.coverage_agent as cov

    async def fake(messages, temperature=0.5, **kw):
        return {"clips": [{"description": "a wide of the hall",
                           "shot_size": "wide", "duration_sec": 4}]}

    monkeypatch.setattr(cov, "generate_structured", fake)
    r = client.post("/api/projects", json={"title": "Novel", "idea": NOVEL})
    pid = r.json()["id"]

    from calliope.config import settings
    from calliope.db import get_db

    conn = get_db(settings.db_path)
    try:
        sid = int(
            conn.execute(
                "INSERT INTO scenes (project_id, order_index, heading) VALUES (?, 1, ?)",
                (pid, "Opening"),
            ).lastrowid
        )
        conn.execute(
            "INSERT INTO clips (project_id, scene_id, order_index) VALUES (?, ?, 1)",
            (pid, sid),
        )
        conn.commit()
    finally:
        conn.close()

    resp = client.post(f"/api/projects/{pid}/scenes/{sid}/expand-clips")
    assert resp.status_code == 200, resp.text
    clips = client.get(f"/api/projects/{pid}/scenes").json()["scenes"][0]["clips"]
    assert any("wide of the hall" in (c.get("description") or "") for c in clips)


def test_coverage_agent_really_does_not_read_idea():
    """The claim above, checked against the source rather than trusted. If
    somebody adds ``idea`` to that prompt later, this is the test that notices
    -- and the guard becomes necessary."""
    from pathlib import Path

    import calliope.agent.coverage_agent as cov_mod

    src = Path(cov_mod.__file__).read_text(encoding="utf-8")
    assert "idea" not in src.lower(), (
        "coverage_agent now mentions `idea` -- re-check whether it reaches the "
        "prompt, and guard it if it does"
    )


# -- wired into ensure_continuity_plan: skip, never raise -------------------


def _board(idea: str) -> dict:
    """``load_board``'s real shape: flat lists, clips at the top level.

    Read from the function rather than assumed -- a board fixture with clips
    nested under scenes raises ``KeyError`` inside ``basis_hash``, which fails
    the test for a reason that has nothing to do with the guard.
    """
    return {
        "project": {"id": 7, "title": "T", "idea": idea, "genre": None,
                    "tone": None, "continuity_json": None},
        "scenes": [{"id": 1, "order_index": 1, "heading": "H",
                    "action": "walks", "dialog": "", "location_id": None}],
        "clips": [{"id": 2, "project_id": 7, "scene_id": 1, "order_index": 1,
                   "description": "a wide", "shot_size": "wide",
                   "duration_sec": 4, "scene_order": 1, "heading": "H"}],
        "characters": [],
        "locations": [],
        "items": [],
    }


def test_continuity_uses_the_board_plan_for_a_novel(monkeypatch):
    """No raise. The render loop calls this, and the render is the user's own
    next step."""
    from calliope.agent import continuity as cont

    board = _board(NOVEL)
    monkeypatch.setattr(cont, "load_board", lambda _pid: board)
    monkeypatch.setattr(cont, "_persist", lambda _pid, _plan: None)

    async def boom(*a, **kw):
        raise AssertionError("the guard let a model call through")

    monkeypatch.setattr(cont, "_llm_plan", boom)
    plan = asyncio.run(cont.ensure_continuity_plan(7))
    assert plan["based_on"]
    assert plan["refreshed"] is True


def test_continuity_still_calls_the_model_for_a_logline(monkeypatch):
    """Same false-positive check as the generators: a guard that fires on
    ordinary projects is worse than no guard, because it is invisible until
    someone notices their story has no plan."""
    from calliope.agent import continuity as cont

    board = _board("a keeper finds a bottle")
    monkeypatch.setattr(cont, "load_board", lambda _pid: board)
    monkeypatch.setattr(cont, "_persist", lambda _pid, _plan: None)

    seen: list[int] = []

    async def fake_llm(_board_arg):
        seen.append(1)
        return {"overview": {"style": "s"}, "shots": []}

    monkeypatch.setattr(cont, "_llm_plan", fake_llm)
    asyncio.run(cont.ensure_continuity_plan(7))
    assert seen == [1], "a logline project stopped reaching the model"


def test_force_does_not_get_past_the_guard(monkeypatch):
    """Forcing past a guard is the caller asking for a worse plan at full
    price. The user-facing "Regenerate" button must not be an escape hatch."""
    from calliope.agent import continuity as cont

    board = _board(NOVEL)
    monkeypatch.setattr(cont, "load_board", lambda _pid: board)
    monkeypatch.setattr(cont, "_persist", lambda _pid, _plan: None)

    async def boom(*a, **kw):
        raise AssertionError("force= bypassed the guard")

    monkeypatch.setattr(cont, "_llm_plan", boom)
    plan = asyncio.run(cont.ensure_continuity_plan(7, force=True))
    assert plan["refreshed"] is True


# -- the other half of the same problem: what a *list* is allowed to carry ----
#
# The guard above stops the model being fed a novel. This stops the novel being
# fed to something that never asked for one: the project list, which reloads on
# every visit and grows with (projects x novel length).


def test_a_preview_fills_the_two_line_card_and_no_more():
    """`ProjectCard.svelte` clamps to two lines, so more than this is invisible
    there -- and in the search box an ellipsis would be a token nobody typed."""
    from calliope.authoring.projects import IDEA_PREVIEW_CHARS, idea_preview

    assert idea_preview("short pitch") == "short pitch"
    assert idea_preview(NOVEL) == NOVEL[:IDEA_PREVIEW_CHARS]
    assert len(idea_preview(NOVEL)) == 200


def test_a_preview_is_not_the_novel():
    from calliope.authoring.projects import idea_preview

    assert idea_preview(NOVEL) != NOVEL
    assert len(idea_preview(NOVEL)) < len(NOVEL) / 10


def test_an_absent_idea_previews_to_itself():
    """`null` must stay `null`: turning "no idea" into ``""`` would make the
    card render its empty-state copy differently from a real empty idea."""
    from calliope.authoring.projects import idea_preview

    assert idea_preview(None) is None
    assert idea_preview("") == ""


def test_the_list_endpoint_does_not_ship_the_novel(client):
    r = client.post("/api/projects", json={"title": "Novel", "idea": NOVEL})
    pid = r.json()["id"]
    row = next(p for p in client.get("/api/projects").json() if p["id"] == pid)
    assert row["idea"] is None
    assert row["idea_preview"] == NOVEL[:200]


def test_the_list_response_does_not_grow_with_the_novel(client):
    """The actual claim being defended: payload size is independent of how long
    the source text is. Compared as sizes, because that is what the browser
    pays -- and a novel is over 10,000 characters here, so the difference is
    unmissable rather than a rounding error."""
    import json

    short = client.post("/api/projects", json={"title": "S", "idea": "x" * 50}).json()["id"]
    long_id = client.post("/api/projects", json={"title": "L", "idea": NOVEL}).json()["id"]
    rows = {p["id"]: p for p in client.get("/api/projects").json()}

    def size(pid):
        return len(json.dumps(rows[pid], ensure_ascii=False))

    # Both rows are the same shape apart from id/title, so the only thing that
    # could differ is the source text -- and it must not.
    assert abs(size(short) - size(long_id)) < 300
    assert len(json.dumps([long_id], ensure_ascii=False)) > 0


def test_the_single_project_response_still_carries_the_novel(client):
    """The non-regression that matters most. The story editor loads this and
    edits it; a preview there would quietly destroy a long source text on the
    first save."""
    pid = client.post("/api/projects", json={"title": "Novel", "idea": NOVEL}).json()["id"]
    detail = client.get(f"/api/projects/{pid}").json()
    assert detail["idea"] == NOVEL
    assert detail["idea_preview"] == NOVEL[:200]


def test_the_cli_list_does_not_dump_the_novel_either(cli_env):
    """Same response, same reason. `project list` is what an agent reads first;
    34k characters of novel at the top of a project's row is context nobody
    spent."""
    run_json = cli_env["json"]
    code, proj, err = run_json("project", "create", "--title", "Novel", "--idea", NOVEL)
    assert code == 0, err
    code, listed, err = run_json("project", "list")
    assert code == 0, err
    row = next(p for p in listed if p["id"] == proj["project_id"])
    assert row["idea"] is None
    assert row["idea_preview"] == NOVEL[:200]


def test_the_cli_still_returns_the_novel_when_asked(cli_env):
    """Truncation is not deletion. `project source` and `project show
    --with-idea` are how the text is actually read."""
    run_json = cli_env["json"]
    code, proj, err = run_json("project", "create", "--title", "Novel", "--idea", NOVEL)
    assert code == 0, err
    code, shown, err = run_json(
        "project", "show", "--project", str(proj["project_id"]), "--with-idea"
    )
    assert code == 0, err
    assert shown["idea"] == NOVEL


# -- what the web UI uses to hide the buttons ------------------------------
#
# The 422s above are the backstop. `ingest_mode` is the front line: a project
# authored through the CLI has its generate buttons removed rather than left
# there to fail. Both stages read `/api/projects/{id}/story`, and both hide a
# different button, so the field has to survive that response.

def test_the_story_response_carries_ingest_mode(client):
    """Both stages read the story endpoint, not the project endpoint. A field
    added only to `GET /api/projects/{id}` would typecheck and then be
    `undefined` at runtime -- falsy, so the buttons would simply never hide."""
    pid = client.post("/api/projects", json={"title": "T", "idea": "x"}).json()["id"]
    assert client.get(f"/api/projects/{pid}/story").json()["project"]["ingest_mode"] == (
        "builtin"
    )


def test_the_project_schema_keeps_ingest_mode(client):
    """`list_projects` has a `response_model`, so anything the schema does not
    declare is silently dropped from the list payload."""
    from calliope.models.schemas import Project

    assert "ingest_mode" in Project.model_fields
    assert Project.model_fields["ingest_mode"].default == "builtin"
    pid = client.post("/api/projects", json={"title": "T", "idea": "x"}).json()["id"]
    row = next(p for p in client.get("/api/projects").json() if p["id"] == pid)
    assert row["ingest_mode"] == "builtin"


def test_an_externally_authored_project_is_labelled_as_such(client, monkeypatch):
    """The value the CLI writes. A UI that cannot tell a CLI-authored project
    from a built-in one will happily offer to overwrite the former."""
    from calliope.config import settings
    from calliope.db import get_db

    pid = client.post("/api/projects", json={"title": "T", "idea": "x"}).json()["id"]
    conn = get_db(settings.db_path)
    try:
        conn.execute(
            "UPDATE projects SET ingest_mode = 'external' WHERE id = ?", (pid,)
        )
        conn.commit()
    finally:
        conn.close()
    assert client.get(f"/api/projects/{pid}/story").json()["project"][
        "ingest_mode"
    ] == "external"


def test_a_web_created_project_is_still_builtin(client):
    """The web form has no ingest-mode control, so pasting a novel there yields
    a `builtin` project. That is the whole reason the length guard exists in
    addition to this flag -- neither signal alone covers every case."""
    pid = client.post("/api/projects", json={"title": "T", "idea": NOVEL}).json()["id"]
    assert client.get(f"/api/projects/{pid}/story").json()["project"]["ingest_mode"] == (
        "builtin"
    )
    assert client.post(f"/api/projects/{pid}/generate-story").status_code == 422
