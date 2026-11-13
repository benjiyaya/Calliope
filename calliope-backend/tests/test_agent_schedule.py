"""schedule() intent cues: one user message becomes the scoped calls it names.

Render intent includes compound names (text2video) and clip/shot words; a
@workflow mention is a canvas cue; scene-origin sessions stay on shot.
"""

from __future__ import annotations

from calliope.agent.runtime.main_agent import handle, requires_scene_write, schedule


def _grants(plans):
    return [plan.grant for plan in plans]


def test_render_request_schedules_render():
    plans = schedule("render all scenes please")
    assert "render" in _grants(plans)
    render_plan = next(plan for plan in plans if plan.grant == "render")
    assert render_plan.write_required is True
    assert render_plan.required_tool == "enqueue_video_jobs"


def test_compound_model_names_signal_render():
    plans = schedule("make a text2video clip of the chase")
    assert "render" in _grants(plans)


def test_clip_words_are_script_cues():
    plans = schedule("break the beats into shot clips")
    assert "script" in _grants(plans)
    script_plan = next(plan for plan in plans if plan.grant == "script")
    assert script_plan.required_tool == "break_into_shots"


def test_scene_write_vs_count_question():
    write = schedule("add a scene where she leaves the valley")
    script_write = next(plan for plan in write if plan.grant == "script")
    assert script_write.required_tool == "add_scene"
    assert script_write.write_required is True

    count = schedule("how many scenes are there?")
    script_read = next(plan for plan in count if plan.grant == "script")
    assert script_read.write_required is False


def test_requires_scene_write_gate():
    assert requires_scene_write("add a scene at the dock") is True
    assert requires_scene_write("how many scenes exist?") is False


def test_workflow_mention_is_canvas_cue():
    cue = (
        "run this one\n\n"
        "[Calliope context]\n"
        'workflow_id=3 name="H3 extend" kind=video — the user picked this '
        "workflow; create a canvas node for it (create_canvas_node with this "
        "workflow_id) and run_canvas_node to prepare the review card."
    )
    plans = schedule(cue)
    assert "canvas" in _grants(plans)
    canvas_plan = next(plan for plan in plans if plan.grant == "canvas")
    assert canvas_plan.required_tool == "create_canvas_node"


def test_plain_canvas_words_schedule_canvas():
    plans = schedule("tidy the canvas, move the character card")
    assert "canvas" in _grants(plans)


def test_wait_cues_wait_on_jobs():
    plans = schedule("wait for the jobs to finish")
    grants = _grants(plans)
    assert "assets" in grants and "render" in grants


def test_message_naming_nothing_schedules_nothing():
    assert schedule("thanks!") == []


def test_scene_origin_stays_on_shot():
    plans = schedule("add a table and a lamp, keyframe a walk", origin="scene")
    assert _grants(plans) == ["shot"]
    # One scoped call; the required_tool is the first named write, and the
    # plan is write-required either way (a prose reply writes nothing).
    assert plans[0].write_required is True
    assert plans[0].required_tool in ("add_object", "add_keyframe")


def test_story_cue_schedules_story():
    plans = schedule("add a beat where the fence breaks")
    assert "story" in _grants(plans)
    story_plan = next(plan for plan in plans if plan.grant == "story")
    assert story_plan.required_tool == "add_beat"


def test_project_creation_schedules_story_then_script():
    plans = schedule("create a new film project from this idea, through the script")
    grants = _grants(plans)
    assert grants[:2] == ["story", "script"]
