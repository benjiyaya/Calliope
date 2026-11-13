"""Main is the only speaker.

It schedules scoped calls, then reads the table that call was allowed to
change. The sentence returned to the user is built from that read — except
for the conversational fallback: a message that names no studio verb gets
one chat call (story when linked, canvas when sandbox), and its sentence
is the model's own reply.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from calliope.agent.capabilities.catalog import live_stat
from calliope.agent.capabilities.scenes import list_scene_heads, project_exists
from calliope.agent.media import format_calliope_context
from calliope.agent.runtime.coordinator import dispatch
from calliope.agent.runtime.types import Call, Run, StreamSink

_WRITE_VERB = re.compile(
    r"\b(add|create|write|append|insert|build|delete|remove|edit|update)\b",
    re.I,
)
_SCENE_WORD = re.compile(r"\bscenes?\b", re.I)
_SCRIPT = re.compile(r"\b(scenes?|clips?|screenplay|scripts?|break into shots)\b", re.I)
_STORY = re.compile(
    r"\b(beats?|characters?|locations?|items?|story|cast|idea|project)\b",
    re.I,
)
_CANVAS = re.compile(r"\bcanvas\b", re.I)
# Render verbs, plus compound model names as substrings — "text2video",
# "fastvideoH3_t2v-API" all signal generation intent (video matches inside
# compound names the way the old render-intent policy taught).
_RENDER = re.compile(r"\b(render|enqueue)\b|[a-z0-9_]*2video|\bt2v\b|\bi2v\b|\bv2v\b", re.I)
_PREVIEW = re.compile(r"\bpreview\b", re.I)
_RENDER_TARGET = re.compile(r"\b(video|clip|scene|film)s?\b", re.I)
_PROJECT = re.compile(
    r"\b(story idea|from (this|an) idea|project creation|through the script)\b"
    r"|\b(create|start|make)\b.{0,40}\b(project|film)\b"
    r"|\bnew (film|project)\b",
    re.I,
)
_ASSETS = re.compile(
    r"\b(portrait|sheet|reference image)s?\b"
    r"|\bgenerate\b.+\b(character|location|item)s?\b.+\bimage\b"
    r"|\bgenerate\b.+\bimage\b.+\b(character|location|item)s?\b",
    re.I,
)
_WAIT = re.compile(r"\bwait\b|when the jobs finish", re.I)
_IMAGE_WAIT = re.compile(r"\b(image|portrait|sheet|character|location|item)s?\b", re.I)
_VIDEO_WAIT = re.compile(r"\b(video|clip|render)s?\b", re.I)
# A status QUESTION ("is the render done?", "check if the portrait is ready")
# must not schedule the enqueue WRITE. The question words must also appear
# before any imperative verb — "what did the render cost" reads, "render the
# scene" writes.
_RENDER_STATUS = re.compile(
    r"\b(is|are|was|were|how|what|check|status|done|finished|ready|progress|yet)\b",
    re.I,
)
_RENDER_IMPERATIVE = re.compile(
    r"\b(generate|enqueue|make|create|produce|start|run|film|animate|re-?render|render)\b",
    re.I,
)
# A @workflow mention lands in the [Calliope context] appendix as
# workflow_id=N — that is a canvas cue even when the prose never says canvas.
_WORKFLOW_MENTION = re.compile(r"^workflow_id=\d+", re.M)


def requires_scene_write(text: str) -> bool:
    """A scene-create ask names a scene and a create verb. A count question does not."""
    create = re.compile(r"\b(add|create|write|append|insert|build)\b", re.I)
    return bool(create.search(text or "") and _SCENE_WORD.search(text or ""))


@dataclass
class TurnResult:
    text: str
    call: Call | None
    task_status: str


@dataclass
class _CallPlan:
    grant: str
    write_required: bool
    required_tool: str | None
    # A conversational call: the sentence comes from the model's reply, not a
    # table read. Only synthesized by handle() when schedule() names nothing.
    chat: bool = False


def _script_tool(text: str) -> str | None:
    if re.search(r"\bbreak\b.+\bshots?\b", text, re.I):
        return "break_into_shots"
    if re.search(r"generate (the )?script|write (the )?script", text, re.I):
        return "generate_script"
    if re.search(r"\b(delete|remove)\b.+\bscenes?\b", text, re.I):
        return "delete_scene"
    if re.search(r"\breorder\b.+\bscenes?\b", text, re.I):
        return "reorder_scenes"
    if re.search(r"\b(edit|update|change|revise)\b.+\bscenes?\b", text, re.I):
        return "update_scene"
    if re.search(r"\b(delete|remove)\b.+\bclips?\b", text, re.I):
        return "delete_clip"
    if re.search(r"\b(edit|update|change)\b.+\bclips?\b", text, re.I):
        return "update_clip"
    if re.search(r"\b(add|create|insert)\b.+\bclips?\b", text, re.I):
        return "add_clip"
    if requires_scene_write(text):
        return "add_scene"
    return None


def _story_tool(text: str) -> str | None:
    if re.search(r"\blink\b.+\bproject\b", text, re.I):
        return "link_project"
    if re.search(
        r"\b(edit|update|change)\b.+\b(project|idea|genre|tone|duration)\b",
        text,
        re.I,
    ):
        return "update_project"
    if re.search(r"generate (the )?story|draft (the )?story|write (the )?story", text, re.I):
        return "generate_story"
    if re.search(r"\b(delete|remove)\b.+\bbeats?\b", text, re.I):
        return "delete_beat"
    if re.search(r"\b(edit|update|change)\b.+\bbeats?\b", text, re.I):
        return "update_beat"
    if re.search(r"\b(add|create|insert|write)\b.+\bbeats?\b", text, re.I):
        return "add_beat"
    if re.search(r"\b(add|create)\b.+\bcharacters?\b", text, re.I):
        return "add_character"
    if re.search(r"\b(add|create)\b.+\blocations?\b", text, re.I):
        return "add_location"
    if re.search(r"\b(add|create)\b.+\bitems?\b", text, re.I):
        return "add_item"
    return None


def _canvas_tool(text: str) -> str | None:
    if re.search(r"\b(connect|wire)\b", text, re.I):
        return "canvas_connect"
    if re.search(r"\b(delete|remove)\b", text, re.I):
        return "delete_canvas_node"
    if re.search(r"\b(add|create|post)\b", text, re.I):
        return "create_canvas_node"
    if _WRITE_VERB.search(text):
        return "update_canvas_node"
    return None


def _shot_tool(text: str) -> str | None:
    if re.search(r"\bkeyframe\b", text, re.I):
        return "add_keyframe"
    if re.search(r"\b(clear|reset)\b", text, re.I):
        return "clear_scene"
    if _WRITE_VERB.search(text):
        return "add_object"
    return None


def _append(plans: list[_CallPlan], plan: _CallPlan) -> None:
    if any(item.grant == plan.grant and item.required_tool == plan.required_tool for item in plans):
        return
    plans.append(plan)


def schedule(goal: str, origin: str = "chat") -> list[_CallPlan]:
    """One user message becomes every scoped call it names, in studio order.

    Shot sessions stay on shot. A sentence that names nothing schedules nothing.
    """
    text = goal or ""
    if origin == "scene":
        tool = _shot_tool(text)
        return [_CallPlan("shot", tool is not None, tool)]
    plans: list[_CallPlan] = []
    project = bool(_PROJECT.search(text))
    render = bool(_RENDER.search(text) and _RENDER_TARGET.search(text))
    preview = bool(_PREVIEW.search(text) and _RENDER_TARGET.search(text))
    # Status question → read plan. The question word must lead the imperative
    # ("is the render done" vs "render the scene"); when no imperative verb
    # appears at all, any question word settles it. For a bare status question
    # the render word itself is the target ("is the render done?" names no
    # video/clip/scene noun).
    status_q = _RENDER_STATUS.search(text)
    imperative = _RENDER_IMPERATIVE.search(text)
    status_read = bool(status_q and (not imperative or status_q.start() < imperative.start()))
    if not render and _RENDER.search(text) and status_read:
        render = True
    script_tool = _script_tool(text) if _SCRIPT.search(text) else None
    script_selected = bool(_SCRIPT.search(text)) and (
        script_tool is not None or not (render or preview)
    )
    story_tool = _story_tool(text) if _STORY.search(text) else None
    # "Create a scene from beat 4" is a script write. The beat is the source.
    scene_from_beat = script_tool == "add_scene" and story_tool == "add_beat"
    if project:
        _append(plans, _CallPlan("story", True, "generate_story"))
        _append(plans, _CallPlan("script", True, "generate_script"))
    elif story_tool is not None and not scene_from_beat:
        _append(plans, _CallPlan("story", True, story_tool))
    elif _STORY.search(text) and not script_selected:
        _append(plans, _CallPlan("story", False, None))
    if script_selected and not project:
        _append(plans, _CallPlan("script", script_tool is not None, script_tool))
    if _ASSETS.search(text):
        if status_read and _IMAGE_WAIT.search(text):
            _append(plans, _CallPlan("assets", False, None))
        else:
            _append(plans, _CallPlan("assets", True, "enqueue_asset_jobs"))
    if render:
        if status_read:
            _append(plans, _CallPlan("render", False, None))
        else:
            _append(plans, _CallPlan("render", True, "enqueue_video_jobs"))
    elif preview:
        _append(plans, _CallPlan("render", False, None))
    if _CANVAS.search(text) or _WORKFLOW_MENTION.search(text):
        tool = _canvas_tool(text)
        _append(plans, _CallPlan("canvas", tool is not None, tool))
    if _WAIT.search(text):
        image = bool(_IMAGE_WAIT.search(text))
        video = bool(_VIDEO_WAIT.search(text))
        if image or not video:
            _append(plans, _CallPlan("assets", False, "wait_for_jobs"))
        if video or not image:
            _append(plans, _CallPlan("render", False, "wait_for_jobs"))
    return plans


def _created_line(rows: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for row in rows:
        heading = (row.get("heading") or "").strip()
        label = f"#{row['order_index']}"
        if heading:
            label = f"{label} {heading}"
        parts.append(f"scene {row['id']} ({label})")
    return "Created " + ", ".join(parts) + "."


def _count_line(project_id: int) -> str:
    count = len(list_scene_heads(project_id))
    noun = "scene" if count == 1 else "scenes"
    return f"The project has {count} {noun}."


def _failure_line(project_id: int) -> str:
    count = len(list_scene_heads(project_id))
    noun = "scene" if count == 1 else "scenes"
    return f"Scene was not created. The project still has {count} {noun}."


def _miss_line(project_id: int | None, grant: str, session_id: int) -> str:
    label, count = live_stat(project_id, grant, session_id)
    noun = label[:-1] if count == 1 and label.endswith("s") else label
    return f"The {grant} was not changed. The project still has {count} {noun}."


def _hit_line(project_id: int | None, grant: str, session_id: int) -> str:
    label, count = live_stat(project_id, grant, session_id)
    noun = label[:-1] if count == 1 and label.endswith("s") else label
    return f"The {grant} changed. The project has {count} {noun}."


async def _emit(callback: Callable[..., Any] | None, *args: Any) -> None:
    if callback is None:
        return
    maybe = callback(*args)
    if asyncio.iscoroutine(maybe):
        await maybe


async def handle(
    session_id: int,
    project_id: int | None,
    goal: str,
    *,
    origin: str = "chat",
    thinking: str | None = None,
    cancel: asyncio.Event | None = None,
    pull_steering: Callable[[], list[str]] | None = None,
    on_tool: Callable[[str, dict[str, Any], dict[str, Any]], Any] | None = None,
    on_plan: Callable[[list[dict[str, Any]], str], Any] | None = None,
    on_task: Callable[..., Any] | None = None,
    stream: StreamSink | None = None,
    attachments: list[dict[str, Any]] | None = None,
    mentions: list[dict[str, Any]] | None = None,
) -> TurnResult:
    # The schedule regex reads the mentions appendix too — a @workflow pick is
    # a canvas cue even when the prose never says "canvas". Attachments are
    # deliberately NOT in the cue: their path/name lines would trip the
    # studio-verb regexes ("portrait.png" → assets write). They reach the
    # model as media parts via dispatch instead.
    cue = goal
    appendix = format_calliope_context(mentions)
    if appendix:
        cue = f"{goal}\n\n{appendix}"
    plans = schedule(cue, origin)
    linked = project_id is not None and project_exists(project_id)
    if not plans:
        # Conversational fallback: the message named no studio verb — plain
        # prose or attachments-only — so schedule one chat call: story reads
        # when the session is linked, canvas tools when it is sandbox. The
        # reply streams like any other call; its sentence is the model's own.
        plans = [_CallPlan("story" if linked else "canvas", False, None, chat=True)]
    creating = bool(_PROJECT.search(goal or ""))
    # Canvas calls work on the sandbox canvas (get-or-create, keyed by the
    # session) — every other grant needs a linked project (story may create
    # one via create_project).
    needs_project = any(plan.grant != "canvas" for plan in plans)
    if origin != "scene" and needs_project and not linked and not creating:
        write = requires_scene_write(goal)
        if write:
            text = "Scene was not created. This chat is not linked to a project."
        else:
            text = "This chat is not linked to a project."
        await _emit(
            on_plan,
            [{"role": "script", "goal": goal, "status": "pending"}],
            "This chat is not linked to a project.",
        )
        await _emit(on_task, 0, "failed", "script")
        return TurnResult(text=text, call=None, task_status="failed")

    await _emit(
        on_plan,
        [
            {"role": plan.grant, "goal": goal, "status": "pending"}
            for plan in plans
        ],
        "Main schedules scoped calls and checks the table each call is allowed to change.",
    )
    run = Run(id=uuid.uuid4().hex, session_id=session_id, project_id=project_id, goal=goal)
    sentences: list[str] = []
    last_call: Call | None = None
    status = "verified"
    for index, plan in enumerate(plans):
        await _emit(on_task, index, "running", plan.grant)
        call, created = await dispatch(
            run,
            goal,
            grant=plan.grant,
            write_required=plan.write_required,
            required_tool=plan.required_tool,
            thinking=thinking,
            cancel=cancel,
            pull_steering=pull_steering,
            on_tool=on_tool,
            stream=stream,
            attachments=attachments,
            mentions=mentions,
        )
        last_call = call
        if call.status == "blocked":
            if plan.chat:
                sentences.append(call.summary.strip() or "Waiting for your answer.")
            else:
                sentences.append(
                    "Review the prompt and the reference media, then press Generate."
                )
            status = "blocked"
            await _emit(on_task, index, "blocked", plan.grant)
            break
        if call.status != "succeeded":
            status = "failed"
            if plan.chat:
                sentences.append("The reply could not be generated.")
            else:
                missed_scene = (
                    plan.grant == "script"
                    and plan.required_tool == "add_scene"
                    and project_id is not None
                )
                if missed_scene:
                    sentences.append(_failure_line(project_id))
                else:
                    sentences.append(_miss_line(project_id, plan.grant, session_id))
            await _emit(on_task, index, "failed", plan.grant)
            break
        if plan.chat:
            sentences.append(call.summary.strip() or "Done.")
        elif plan.grant == "script" and created and project_id is not None:
            sentences.append(_created_line(created))
        elif not plan.write_required and plan.grant == "script" and project_id is not None:
            sentences.append(_count_line(project_id))
        elif not plan.write_required:
            label, count = live_stat(project_id, plan.grant, session_id)
            noun = label[:-1] if count == 1 and label.endswith("s") else label
            if plan.required_tool == "wait_for_jobs" and call.summary.strip():
                # "Wait for the render" ends with the model's outcome report —
                # a job count is not what the user asked to hear.
                sentences.append(call.summary.strip())
            else:
                sentences.append(f"The project has {count} {noun}.")
        else:
            sentences.append(_hit_line(project_id, plan.grant, session_id))
        await _emit(on_task, index, "verified", plan.grant)
    return TurnResult(text=" ".join(sentences), call=last_call, task_status=status)
