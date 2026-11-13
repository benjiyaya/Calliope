"""Tool grants for the six studio calls."""

from __future__ import annotations

import json
from typing import Any

from calliope.agent.capabilities import beats, canvas_ops, cast, clips, shot_ops
from calliope.agent.capabilities.scenes import (
    add_scene,
    get_beat,
    get_scene,
    list_beats,
    list_scenes,
)
from calliope.agent.capabilities.sql import as_int, connect

GRANTS: dict[str, frozenset[str]] = {
    "story": frozenset(
        {
            "list_beats",
            "get_beat",
            "add_beat",
            "update_beat",
            "delete_beat",
            "list_characters",
            "add_character",
            "update_character",
            "delete_character",
            "list_locations",
            "add_location",
            "update_location",
            "delete_location",
            "list_items",
            "add_item",
            "update_item",
            "delete_item",
            "generate_story",
            "create_project",
            "update_project",
            "link_project",
            "list_skills",
            "read_skill",
            "ask_user",
        }
    ),
    "script": frozenset(
        {
            "list_beats",
            "get_beat",
            "list_scenes",
            "add_scene",
            "update_scene",
            "delete_scene",
            "reorder_scenes",
            "list_clips",
            "add_clip",
            "update_clip",
            "delete_clip",
            "break_into_shots",
            "generate_script",
            "get_scene",
            "list_skills",
            "read_skill",
            "ask_user",
        }
    ),
    "assets": frozenset(
        {
            "list_characters",
            "list_locations",
            "list_items",
            "enqueue_asset_jobs",
            "attach_image",
            "wait_for_jobs",
            "list_jobs",
            "list_skills",
            "read_skill",
            "ask_user",
        }
    ),
    "render": frozenset(
        {
            "list_scenes",
            "list_clips",
            "preview_clip_prompt",
            "enqueue_video_jobs",
            "wait_for_jobs",
            "list_jobs",
            "list_workflows",
            "list_skills",
            "read_skill",
            "ask_user",
        }
    ),
    "canvas": frozenset(
        {
            "summarize_canvas",
            "create_canvas_node",
            "update_canvas_node",
            "delete_canvas_node",
            "canvas_connect",
            "canvas_link",
            "post_artifact_to_canvas",
            "run_canvas_node",
            "list_workflows",
            "list_skills",
            "read_skill",
            "ask_user",
        }
    ),
    "shot": frozenset(shot_ops._SHOT_NAMES)
    | frozenset({"list_skills", "read_skill", "ask_user"}),
}

_MEMORY = frozenset({"save_memory", "list_memories", "forget_memory"})
# Scratch-file tools + the shell: available in every chat grant (shell is
# additionally gated at execute time by settings.agent_shell_enabled and the
# HITL approval chain in capabilities/shell.py). Build Scene (shot) stays
# shot-only — advertising file tools there would invite 'tool doesn't exist'
# loops, since its grant is the session's whole toolset.
_SYSTEM = frozenset(
    {"run_command", "list_files", "read_file", "write_file", "delete_file"}
)
GRANTS = {
    name: tools | _MEMORY | (_SYSTEM if name != "shot" else frozenset())
    for name, tools in GRANTS.items()
}

_PROMPTS = {
    "story": (
        "You edit the story bible: beats, characters, locations, and items. "
        "For a new film, read_skill project-creation. If this chat is not linked, "
        "call create_project with the title and the idea before any other write. "
        "Then write the beats and cast, and stop. "
        "The screenplay is a later call. "
        "add_beat, update_beat, delete_beat, add_character, add_location, add_item, "
        "and generate_story write rows. A reply of Done does not."
    ),
    "script": (
        "You edit the screenplay and its shot clips. "
        "For a new film, read_skill project-creation, write the screenplay, and stop. "
        "Do not enqueue images or video. "
        "get_beat before add_scene. add_scene, update_scene, delete_scene, "
        "add_clip, update_clip, delete_clip, break_into_shots, and generate_script "
        "write rows. A reply of Done does not. The last clip on a scene cannot be deleted."
    ),
    "assets": (
        "You prepare reference-image jobs for characters, locations, and items. "
        "enqueue_asset_jobs returns a review of the prompt and reference images and "
        "does not queue a job. Stop when it does. The user presses Generate. "
        "A reply of Done does not queue a job."
    ),
    "render": (
        "You prepare video jobs for clips. read_skill video-continuation before a clip "
        "that continues the previous place. enqueue_video_jobs returns a review of the "
        "prompt, images, audio, and video and does not queue a job. Stop when it does. "
        "The user presses Generate. preview_clip_prompt does not render. "
        "A reply of Done does not queue a job."
    ),
    "canvas": (
        "You edit the canvas. create_canvas_node, update_canvas_node, delete_canvas_node, "
        "canvas_connect, canvas_link, post_artifact_to_canvas, and run_canvas_node write. "
        "summarize_canvas first. A reply of Done does not."
    ),
    "shot": (
        "You build the 3D blockout. get_scene, then add_object, set_transform, "
        "add_keyframe, and set_shot. A reply of Done does not change the composition."
    ),
}


def _schema(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "additionalProperties": False,
            },
        },
    }


def _str(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


def _int(description: str) -> dict[str, str]:
    return {"type": "integer", "description": description}


_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_beats": _schema("list_beats", "Beat order and title. No descriptions.", {}),
    "get_beat": _schema(
        "get_beat",
        "One beat, full description. Pass order or title.",
        {"order": _int("order_index"), "title": _str("exact title")},
    ),
    "add_beat": _schema(
        "add_beat",
        "Insert a beat.",
        {
            "title": _str("required"),
            "description": _str("what happens"),
            "order_index": _int("optional insert position"),
        },
    ),
    "update_beat": _schema(
        "update_beat",
        "Edit a beat.",
        {
            "beat_id": _int("id"),
            "title": _str("title"),
            "description": _str("description"),
            "order_index": _int("order"),
        },
    ),
    "delete_beat": _schema("delete_beat", "Delete a beat.", {"beat_id": _int("id")}),
    "list_characters": _schema("list_characters", "Characters in the project.", {}),
    "add_character": _schema(
        "add_character",
        "Add a character.",
        {
            "name": _str("required"),
            "role": _str("role"),
            "age": _str("age"),
            "appearance": _str("appearance"),
            "personality": _str("personality"),
            "consistency_prompt": _str("image prompt"),
        },
    ),
    "update_character": _schema(
        "update_character",
        "Edit a character.",
        {
            "character_id": _int("id"),
            "name": _str("name"),
            "role": _str("role"),
            "appearance": _str("appearance"),
            "personality": _str("personality"),
            "consistency_prompt": _str("image prompt"),
        },
    ),
    "delete_character": _schema(
        "delete_character", "Delete a character.", {"character_id": _int("id")}
    ),
    "list_locations": _schema("list_locations", "Locations in the project.", {}),
    "add_location": _schema(
        "add_location",
        "Add a location.",
        {
            "name": _str("required"),
            "description": _str("description"),
            "consistency_prompt": _str("image prompt"),
        },
    ),
    "update_location": _schema(
        "update_location",
        "Edit a location.",
        {
            "location_id": _int("id"),
            "name": _str("name"),
            "description": _str("description"),
            "consistency_prompt": _str("image prompt"),
        },
    ),
    "delete_location": _schema(
        "delete_location", "Delete a location.", {"location_id": _int("id")}
    ),
    "list_items": _schema("list_items", "Misc items in the project.", {}),
    "add_item": _schema(
        "add_item",
        "Add an item.",
        {
            "name": _str("required"),
            "description": _str("description"),
            "consistency_prompt": _str("image prompt"),
        },
    ),
    "update_item": _schema(
        "update_item",
        "Edit an item.",
        {
            "item_id": _int("id"),
            "name": _str("name"),
            "description": _str("description"),
            "consistency_prompt": _str("image prompt"),
        },
    ),
    "delete_item": _schema("delete_item", "Delete an item.", {"item_id": _int("id")}),
    "create_project": _schema(
        "create_project",
        "Create a project from a sandbox chat and link this chat to it.",
        {
            "title": _str("project title"),
            "idea": _str("story idea"),
            "genre": _str("optional genre"),
            "tone": _str("optional tone"),
            "target_duration": _str("optional duration"),
        },
    ),
    "update_project": _schema(
        "update_project",
        "Edit the linked project's title, idea, genre, tone, or duration.",
        {
            "title": _str("title"),
            "idea": _str("idea"),
            "genre": _str("genre"),
            "tone": _str("tone"),
            "target_duration": _str("duration"),
        },
    ),
    "link_project": _schema(
        "link_project",
        "Link this chat to a project that already exists.",
        {"project_id": _int("project id")},
    ),
    "get_scene": _schema(
        "get_scene",
        "One scene in full: action, dialogue, and clips.",
        {"scene_id": _int("id"), "order": _int("order_index")},
    ),
    "attach_image": _schema(
        "attach_image",
        "File an existing image onto a character, location, or item. Does not queue.",
        {
            "entity": _str("character, location, or item"),
            "id": _int("entity id"),
            "path": _str("file under the assets folder"),
            "target": _str("sheet or portrait, characters only"),
        },
    ),
    "wait_for_jobs": _schema(
        "wait_for_jobs",
        "Wait until image or video jobs finish. Does not queue.",
        {"job_ids": {"type": "array", "items": {"type": "integer"}}},
    ),
    "save_memory": _schema(
        "save_memory",
        "Remember a durable preference, convention, or correction.",
        {
            "content": _str("what to remember"),
            "scope": _str("global or project"),
            "kind": _str("preference, convention, or correction"),
        },
    ),
    "list_memories": _schema("list_memories", "Memories for this project and global ones.", {}),
    "forget_memory": _schema(
        "forget_memory",
        "Delete a memory by id or exact content.",
        {"memory_id": _int("id"), "content": _str("exact text")},
    ),
    "generate_story": _schema(
        "generate_story",
        "Draft beats and cast from the project idea and insert them.",
        {"replace": {"type": "boolean", "description": "delete the current board first"}},
    ),
    "list_scenes": _schema("list_scenes", "Scenes: id, order, heading, short action.", {}),
    "add_scene": _schema(
        "add_scene",
        "Insert one scene and its default clip.",
        {
            "heading": _str("slugline"),
            "action": _str("action prose"),
            "dialog": _str("SPEAKER: line"),
            "duration_sec": _int("seconds"),
            "beat_id": _int("source beat id"),
        },
    ),
    "update_scene": _schema(
        "update_scene",
        "Edit a scene by scene_id or order.",
        {
            "scene_id": _int("id"),
            "order": _int("order_index"),
            "heading": _str("heading"),
            "action": _str("action"),
            "dialog": _str("dialog"),
            "duration_sec": _int("seconds"),
            "beat_id": _int("beat"),
        },
    ),
    "delete_scene": _schema(
        "delete_scene",
        "Delete a scene and its clips.",
        {"scene_id": _int("id"), "order": _int("order_index")},
    ),
    "reorder_scenes": _schema(
        "reorder_scenes",
        "Set scene order.",
        {"scene_ids": {"type": "array", "items": {"type": "integer"}}},
    ),
    "list_clips": _schema(
        "list_clips",
        "Clips with #scene.clip labels.",
        {"scene_id": _int("optional filter")},
    ),
    "add_clip": _schema(
        "add_clip",
        "Add a shot clip to a scene.",
        {
            "scene_id": _int("id"),
            "order": _int("scene order"),
            "description": _str("what the camera sees"),
            "shot_size": _str("wide, medium, closeUp, insert, overShoulder"),
            "duration_sec": _int("seconds"),
        },
    ),
    "update_clip": _schema(
        "update_clip",
        "Edit a clip by clip_id or #scene.clip ref.",
        {
            "clip_id": _int("id"),
            "ref": _str("#3.2"),
            "description": _str("description"),
            "shot_size": _str("shot size"),
            "duration_sec": _int("seconds"),
            "prompt": _str("saved video prompt"),
            "workflow_id": _int("workflow id"),
            "images": {"type": "array", "items": {"type": "string"}},
            "audio": {"type": "array", "items": {"type": "string"}},
            "video": {"type": "array", "items": {"type": "string"}},
        },
    ),
    "delete_clip": _schema(
        "delete_clip",
        "Delete a clip. The last clip on a scene stays.",
        {"clip_id": _int("id"), "ref": _str("#3.2")},
    ),
    "break_into_shots": _schema(
        "break_into_shots",
        "Replace a scene's clips with coverage clips.",
        {
            "scene_id": _int("id"),
            "order": _int("scene order"),
            "all_scenes": {"type": "boolean"},
            "guidance": _str("optional note"),
        },
    ),
    "generate_script": _schema(
        "generate_script",
        "Write screenplay scenes from the beats.",
        {"replace": {"type": "boolean"}, "scene_count": _int("optional count")},
    ),
    "enqueue_asset_jobs": _schema(
        "enqueue_asset_jobs",
        "Queue reference images. Pass ids or all_missing.",
        {
            "character_ids": {"type": "array", "items": {"type": "integer"}},
            "location_ids": {"type": "array", "items": {"type": "integer"}},
            "item_ids": {"type": "array", "items": {"type": "integer"}},
            "all_missing": {"type": "boolean"},
        },
    ),
    "preview_clip_prompt": _schema(
        "preview_clip_prompt",
        "Resolve the prompt for one clip. Does not render.",
        {"clip_id": _int("id")},
    ),
    "enqueue_video_jobs": _schema(
        "enqueue_video_jobs",
        "Queue video renders for clip ids.",
        {"clip_ids": {"type": "array", "items": {"type": "integer"}}},
    ),
    "list_skills": _schema("list_skills", "Names and descriptions of built-in skills.", {}),
    "read_skill": _schema(
        "read_skill",
        "Read one skill file. Use SKILL.md first.",
        {
            "name": _str("skill folder name"),
            "path": _str("file inside the skill, default SKILL.md"),
        },
    ),
    "ask_user": _schema(
        "ask_user",
        "Pause and ask the user. Use this for a decision that is not a generation review.",
        {
            "question": _str("the question"),
            "options": {"type": "array", "items": {"type": "string"}},
            "scope": _str("info, render, or destructive_replace"),
        },
    ),
    "summarize_canvas": _schema("summarize_canvas", "Nodes and edges on this canvas.", {}),
    "create_canvas_node": _schema(
        "create_canvas_node",
        "Add a canvas card.",
        {
            "type": _str("text, workflow, image, video, or entity"),
            "title": _str("title"),
            "x": {"type": "number"},
            "y": {"type": "number"},
            "workflow_id": _int("workflow"),
        },
    ),
    "update_canvas_node": _schema(
        "update_canvas_node",
        "Move or retitle a node.",
        {
            "node_id": _int("id"),
            "title": _str("title"),
            "x": {"type": "number"},
            "y": {"type": "number"},
        },
    ),
    "delete_canvas_node": _schema(
        "delete_canvas_node", "Remove a canvas card.", {"node_id": _int("id")}
    ),
    "canvas_connect": _schema(
        "canvas_connect",
        "Data edge into a workflow role.",
        {
            "src_node_id": _int("source"),
            "dst_node_id": _int("destination"),
            "dst_role": _str("Input role"),
        },
    ),
    "canvas_link": _schema(
        "canvas_link",
        "Labeled story link.",
        {
            "src_node_id": _int("source"),
            "dst_node_id": _int("destination"),
            "label": _str("label"),
        },
    ),
    "post_artifact_to_canvas": _schema(
        "post_artifact_to_canvas",
        "Post a finished job's output (or an asset file) onto the canvas as a "
        "card. Pass job_id (resolves the first output; deduplicated) or "
        "asset_path.",
        {
            "job_id": _int("finished job — its first output becomes the card"),
            "asset_path": _str("file path (extension decides image vs video)"),
            "title": _str("card title"),
        },
    ),
    "run_canvas_node": _schema(
        "run_canvas_node",
        "Mark a workflow node running and queue its job when it has a workflow.",
        {"node_id": _int("id")},
    ),
    "run_command": _schema(
        "run_command",
        "Run an installed command-line executable INSIDE the agent workspace "
        "folder (Settings → System Paths). args is an argv list — never a "
        "shell string; shell interpreters (bash, powershell, …) are blocked. "
        "Read-only inspection (ffprobe --version) runs directly; anything "
        "else needs the user's approval first (ask_user scope='shell'). "
        "Disabled unless the operator enabled the agent shell in Settings.",
        {
            "command": _str("bare executable name, e.g. 'ffprobe' — no paths, no shell"),
            "args": {"type": "array", "items": {"type": "string"}},
            "cwd": _str("optional working dir; default and only allowed root is the agent workspace"),
            "timeout_sec": {"type": "number"},
        },
    ),
    "list_files": _schema(
        "list_files",
        "List files in this session's scratch workspace folder "
        "(workspace/sessions/<session id>). Pass no arguments for the root.",
        {"subdir": _str("optional subfolder, relative to the session folder")},
    ),
    "read_file": _schema(
        "read_file",
        "Read a text file from this session's scratch workspace. path is "
        "relative to that folder — list_files shows what exists.",
        {"path": _str("relative file path")},
    ),
    "write_file": _schema(
        "write_file",
        "Write a text draft into this session's scratch workspace — scripts, "
        "beat lists, shot plans, video prompts. Show drafts to the user, then "
        "commit approved content with the scene/clip/beat tools.",
        {
            "path": _str("relative file path"),
            "content": _str("UTF-8 text to write"),
            "mode": _str("overwrite (default) or append"),
        },
    ),
    "delete_file": _schema(
        "delete_file",
        "Delete one file from this session's scratch workspace (files only).",
        {"path": _str("relative file path")},
    ),
    "list_workflows": _schema(
        "list_workflows",
        "Imported ComfyUI workflows (id, name, kind, enabled, prompt profile). "
        "The user picks one with a @workflow mention; you prepare a canvas "
        "node + review for it.",
        {},
    ),
    "list_jobs": _schema(
        "list_jobs",
        "Recent jobs for the linked project with status and errors.",
        {"limit": _int("max rows, default 20")},
    ),
}

for _shot_name in shot_ops._SHOT_NAMES:
    _SCHEMAS.setdefault(
        _shot_name,
        _schema(
            _shot_name,
            f"Build Scene: {_shot_name}.",
            {
                "object_id": _str("object id from get_scene"),
                "id": _str("new object id"),
                "name": _str("name"),
                "kind": _str("block or figure"),
                "joint": _str("joint name"),
                "keyframe_id": _str("keyframe id"),
                "time": {"type": "number"},
                "angle": _str("camera angle"),
                "pose": _str("pose name"),
            },
        ),
    )


def tools_for(grant: str) -> list[dict[str, Any]]:
    names = GRANTS.get(grant, GRANTS["script"])
    return [_SCHEMAS[name] for name in sorted(names) if name in _SCHEMAS]


def _list_workflows() -> dict[str, Any]:
    """Discovery for the canvas/review flow — the agent never enqueues
    directly; a workflow pick becomes a canvas node + review card."""
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT id, name, kind, is_enabled, prompt_profile, description
            FROM workflows ORDER BY id
            """
        ).fetchall()
        return {
            "ok": True,
            "workflows": [
                {
                    "id": int(row["id"]),
                    "name": row["name"],
                    "kind": row["kind"],
                    "enabled": bool(row["is_enabled"]),
                    "prompt_profile": row["prompt_profile"],
                    "description": row["description"],
                }
                for row in rows
            ],
        }
    finally:
        conn.close()


def _list_jobs(project_id: int | None, args: dict[str, Any]) -> dict[str, Any]:
    if project_id is None:
        return {"ok": False, "error": "list_jobs needs a linked project"}
    try:
        limit = max(1, min(50, int(args.get("limit") or 20)))
    except (TypeError, ValueError):
        limit = 20
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT id, kind, status, scene_id, clip_id, error, created_at, completed_at
            FROM jobs WHERE project_id = ? ORDER BY id DESC LIMIT ?
            """,
            (project_id, limit),
        ).fetchall()
        return {
            "ok": True,
            "jobs": [
                {
                    "id": int(row["id"]),
                    "kind": row["kind"],
                    "status": row["status"],
                    "scene_id": row["scene_id"],
                    "clip_id": row["clip_id"],
                    "error": row["error"],
                    "created_at": row["created_at"],
                    "completed_at": row["completed_at"],
                }
                for row in rows
            ],
        }
    finally:
        conn.close()


def prompt_for(grant: str) -> str:
    return _PROMPTS.get(grant, _PROMPTS["script"]) + _DISCIPLINE


# Shared across every grant: the write-trigger is load-bearing (the tools do
# nothing unless the model is TOLD to notice and save), and skills teach the
# style that prompt_format alone cannot.
_DISCIPLINE = (
    " MEMORY: the moment the user states a durable preference or corrects a "
    "recurring behavior, call save_memory — it is recalled into every future "
    "prompt. SKILLS: list_skills shows what exists; read_skill the matching "
    "one before long-form writing."
)


def empty_story_fingerprint(project_id: int) -> str:
    """Story fingerprint of a just-linked project before this call wrote rows."""
    conn = connect()
    try:
        project = conn.execute(
            """
            SELECT title, idea, genre, tone, target_duration
            FROM projects WHERE id = ?
            """,
            (project_id,),
        ).fetchone()
        rows = [[], [], [], [], dict(project) if project else {}]
        return json.dumps(rows, sort_keys=True, default=str)
    finally:
        conn.close()


def fingerprint(project_id: int | None, grant: str, session_id: int | None) -> str:
    conn = connect()
    try:
        if grant == "story" and project_id is not None:
            rows = []
            for table in ("story_beats", "characters", "locations", "items"):
                found = conn.execute(
                    f"SELECT * FROM {table} WHERE project_id = ? ORDER BY id",
                    (project_id,),
                ).fetchall()
                rows.append([dict(row) for row in found])
            project = conn.execute(
                """
                SELECT title, idea, genre, tone, target_duration
                FROM projects WHERE id = ?
                """,
                (project_id,),
            ).fetchone()
            rows.append(dict(project) if project else {})
        elif grant == "script" and project_id is not None:
            scenes = conn.execute(
                "SELECT * FROM scenes WHERE project_id = ? ORDER BY id", (project_id,)
            ).fetchall()
            found_clips = conn.execute(
                "SELECT * FROM clips WHERE project_id = ? ORDER BY id", (project_id,)
            ).fetchall()
            rows = [[dict(row) for row in scenes], [dict(row) for row in found_clips]]
        elif grant in ("assets", "render") and project_id is not None:
            found = conn.execute(
                """
                SELECT id, kind, status, scene_id, clip_id
                FROM jobs WHERE project_id = ? ORDER BY id
                """,
                (project_id,),
            ).fetchall()
            rows = [dict(row) for row in found]
        elif grant == "canvas":
            board = canvas_ops.canvas_for(project_id, session_id)
            nodes = conn.execute(
                """
                SELECT id, type, title, x, y, status, deleted
                FROM canvas_node WHERE canvas_id = ? ORDER BY id
                """,
                (board["id"],),
            ).fetchall()
            edges = conn.execute(
                """
                SELECT id, src_node_id, dst_node_id, kind, label, dst_role
                FROM canvas_edge WHERE canvas_id = ? ORDER BY id
                """,
                (board["id"],),
            ).fetchall()
            rows = [[dict(row) for row in nodes], [dict(row) for row in edges]]
        elif grant == "shot" and session_id is not None:
            row = conn.execute(
                "SELECT scene_json FROM shot_composition WHERE agent_session_id = ?",
                (session_id,),
            ).fetchone()
            rows = row["scene_json"] if row else ""
        else:
            # An unlinked story call fingerprints like an empty project, so
            # create_project alone is not a story-table change.
            rows = [[], [], [], []] if grant == "story" else []
        return json.dumps(rows, sort_keys=True, default=str)
    finally:
        conn.close()


def live_stat(project_id: int | None, grant: str, session_id: int | None) -> tuple[str, int]:
    conn = connect()
    try:
        if grant == "story" and project_id is not None:
            count = conn.execute(
                "SELECT COUNT(*) FROM story_beats WHERE project_id = ?", (project_id,)
            ).fetchone()[0]
            return "beats", int(count)
        if grant in ("assets", "render") and project_id is not None:
            count = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE project_id = ?", (project_id,)
            ).fetchone()[0]
            return "jobs", int(count)
        if grant == "canvas":
            board = canvas_ops.canvas_for(project_id, session_id)
            count = conn.execute(
                "SELECT COUNT(*) FROM canvas_node WHERE canvas_id = ? AND deleted = 0",
                (board["id"],),
            ).fetchone()[0]
            return "nodes", int(count)
        if grant == "shot" and session_id is not None:
            row = conn.execute(
                "SELECT scene_json FROM shot_composition WHERE agent_session_id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return "objects", 0
            try:
                data = json.loads(row["scene_json"] or "{}")
            except json.JSONDecodeError:
                data = {}
            return "objects", len(data.get("objects") or [])
        if project_id is None:
            return "scenes", 0
        count = conn.execute(
            "SELECT COUNT(*) FROM scenes WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        return "scenes", int(count)
    finally:
        conn.close()


async def create_linked_project(
    session_id: int | None, args: dict[str, Any]
) -> dict[str, Any]:
    """Create a project from a sandbox chat and link that chat to it.

    Publishes agent.session.updated so the canvas can leave the sandbox
    board and open the project canvas without a browser reload.
    """
    from calliope.authoring.projects import create_project
    from calliope.authoring.service import ValidationFailed
    from calliope.events.bus import event_bus

    title = str(args.get("title") or args.get("name") or "").strip()
    if not title:
        return {"ok": False, "error": "title is required"}
    title = title[:200]
    conn = connect()
    try:
        if session_id is not None:
            current = conn.execute(
                "SELECT project_id FROM agent_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if current is not None and current["project_id"] is not None:
                project_id = int(current["project_id"])
                canvas = conn.execute(
                    "SELECT id FROM canvas WHERE project_id = ? ORDER BY id LIMIT 1",
                    (project_id,),
                ).fetchone()
                return {
                    "ok": True,
                    "already_linked": True,
                    "project_id": project_id,
                    "canvas_id": int(canvas["id"]) if canvas else None,
                }
        try:
            project_id = create_project(
                conn,
                title=title,
                idea=(str(args.get("idea")).strip() if args.get("idea") else None),
                genre=(str(args.get("genre")).strip() if args.get("genre") else None),
                tone=(str(args.get("tone")).strip() if args.get("tone") else None),
                target_duration=(
                    str(args.get("target_duration")).strip()
                    if args.get("target_duration")
                    else None
                ),
            )
        except ValidationFailed as exc:
            return {"ok": False, "error": str(exc)}
        if session_id is not None:
            conn.execute(
                """
                UPDATE agent_sessions
                SET project_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND project_id IS NULL
                """,
                (project_id, session_id),
            )
            named = conn.execute(
                "SELECT title FROM agent_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if named is not None and (named["title"] or "").strip() in ("", "New chat"):
                conn.execute(
                    "UPDATE agent_sessions SET title = ? WHERE id = ?",
                    (title[:300], session_id),
                )
        existing = conn.execute(
            "SELECT id FROM canvas WHERE project_id = ? ORDER BY id LIMIT 1",
            (project_id,),
        ).fetchone()
        if existing is not None:
            canvas_id = int(existing["id"])
        else:
            cur = conn.execute(
                """
                INSERT INTO canvas (project_id, title, created_at, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (project_id, title[:60]),
            )
            canvas_id = int(cur.lastrowid)
        conn.commit()
        session = None
        if session_id is not None:
            row = conn.execute(
                "SELECT * FROM agent_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            project = conn.execute(
                "SELECT id, title, status FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if row is not None:
                from calliope.db import row_to_dict

                session = row_to_dict(row)
                session["project"] = row_to_dict(project) if project else None
                session["running"] = True
    finally:
        conn.close()
    if session is not None:
        await event_bus.publish("agent.session.updated", {"session": session})
    await event_bus.publish(
        "canvas.updated",
        {"canvas_id": canvas_id, "project_id": project_id, "reason": "project_created"},
    )
    return {
        "ok": True,
        "project_id": project_id,
        "canvas_id": canvas_id,
        "title": title,
    }


async def execute(
    name: str,
    arguments: dict[str, Any],
    project_id: int | None,
    session_id: int | None,
) -> dict[str, Any]:
    args = arguments or {}
    if name == "list_skills":
        from calliope.agent.skills_store import list_skills

        return {"ok": True, "skills": list_skills()}
    if name == "read_skill":
        from calliope.agent.skills_store import read_skill_file

        return read_skill_file(str(args.get("name") or ""), str(args.get("path") or "SKILL.md"))
    if name == "ask_user":
        question = str(args.get("question") or "").strip()
        raw_options = args.get("options") if isinstance(args.get("options"), list) else []
        options = [str(item).strip() for item in raw_options if str(item).strip()][:6]
        if len(options) < 2:
            options = ["Yes", "No"]
        if not question:
            return {"ok": False, "error": "question is required"}
        return {
            "ok": True,
            "awaiting_input": True,
            "question": question,
            "question_seq": 0,
            "options": options,
            "scope": str(args.get("scope") or "info"),
        }
    if name == "create_project":
        return await create_linked_project(session_id, args)
    if name == "link_project":
        from calliope.agent.capabilities.project_ops import link_project

        return await link_project(session_id, args)
    if name in ("save_memory", "list_memories", "forget_memory"):
        from calliope.agent.capabilities import memory

        if name == "save_memory":
            return memory.save_memory(project_id, args)
        if name == "list_memories":
            return memory.list_memories(project_id, args)
        return memory.forget_memory(project_id, args)
    if name == "run_command":
        from calliope.agent.capabilities import shell

        return await shell.run_command(session_id, args)
    if name in ("list_files", "read_file", "write_file", "delete_file"):
        from calliope.agent.capabilities import workspace

        fn = getattr(workspace, name)
        return await fn(session_id, args)
    if name == "list_workflows":
        return _list_workflows()
    if name == "list_jobs":
        return _list_jobs(project_id, args)
    if name == "get_scene" and project_id is None:
        return shot_ops.execute(session_id, name, args)
    if name in shot_ops._SHOT_NAMES and name != "get_scene":
        return shot_ops.execute(session_id, name, args)
    if project_id is None and name not in GRANTS["canvas"]:
        return {"ok": False, "error": "This chat is not linked to a project"}
    if name == "update_project":
        from calliope.agent.capabilities.project_ops import update_project

        return update_project(project_id, args)
    if name == "wait_for_jobs":
        from calliope.agent.capabilities.jobs import wait_for_jobs

        return await wait_for_jobs(project_id, args)
    if name == "attach_image":
        return cast.attach_image(project_id, args)
    if name == "list_beats":
        return list_beats(project_id)
    if name == "get_beat":
        return get_beat(project_id, order=args.get("order"), title=args.get("title"))
    if name == "add_beat":
        return beats.add_beat(project_id, args)
    if name == "update_beat":
        return beats.update_beat(project_id, args)
    if name == "delete_beat":
        return beats.delete_beat(project_id, args)
    if name == "list_characters":
        return cast.list_entities(project_id, "character")
    if name == "add_character":
        return cast.add_entity(project_id, "character", args)
    if name == "update_character":
        return cast.update_entity(project_id, "character", args)
    if name == "delete_character":
        return cast.delete_entity(project_id, "character", args)
    if name == "list_locations":
        return cast.list_entities(project_id, "location")
    if name == "add_location":
        return cast.add_entity(project_id, "location", args)
    if name == "update_location":
        return cast.update_entity(project_id, "location", args)
    if name == "delete_location":
        return cast.delete_entity(project_id, "location", args)
    if name == "list_items":
        return cast.list_entities(project_id, "item")
    if name == "add_item":
        return cast.add_entity(project_id, "item", args)
    if name == "update_item":
        return cast.update_entity(project_id, "item", args)
    if name == "delete_item":
        return cast.delete_entity(project_id, "item", args)
    if name == "generate_story":
        from fastapi import HTTPException

        from calliope.agent.idea_guard import LongSourceText
        from calliope.routers.story import generate_story

        try:
            return await generate_story(project_id, replace=bool(args.get("replace")))
        except HTTPException as exc:
            return {"ok": False, "error": str(exc.detail)}
        except LongSourceText as exc:
            return {"ok": False, "error": str(exc)}
    if name == "list_scenes":
        return list_scenes(project_id)
    if name == "get_scene":
        return get_scene(
            project_id,
            scene_id=as_int(args.get("scene_id")),
            order=as_int(args.get("order")),
        )
    if name == "add_scene":
        return add_scene(
            project_id,
            heading=args.get("heading"),
            action=args.get("action"),
            dialog=args.get("dialog"),
            duration_sec=args.get("duration_sec"),
            beat_id=args.get("beat_id"),
        )
    if name == "update_scene":
        return clips.update_scene(project_id, args)
    if name == "delete_scene":
        return clips.delete_scene(project_id, args)
    if name == "reorder_scenes":
        return clips.reorder_scenes(project_id, args)
    if name == "list_clips":
        return clips.list_clips(project_id, args)
    if name == "add_clip":
        return clips.add_clip(project_id, args)
    if name == "update_clip":
        return clips.update_clip(project_id, args)
    if name == "delete_clip":
        return clips.delete_clip(project_id, args)
    if name == "break_into_shots":
        from calliope.agent.coverage_agent import expand_scene_coverage

        scene_id = as_int(args.get("scene_id"))
        if scene_id is None and args.get("order") is not None:
            conn = connect()
            try:
                row = conn.execute(
                    "SELECT id FROM scenes WHERE project_id = ? AND order_index = ?",
                    (project_id, as_int(args.get("order"))),
                ).fetchone()
            finally:
                conn.close()
            scene_id = int(row["id"]) if row else None
        if args.get("all_scenes"):
            ids = None
        elif scene_id is not None:
            ids = [scene_id]
        else:
            return {"ok": False, "error": "Pass scene_id, order, or all_scenes"}
        return await expand_scene_coverage(project_id, ids, guidance=args.get("guidance"))
    if name == "generate_script":
        from calliope.agent.script_agent import generate_script

        return await generate_script(
            project_id,
            replace=bool(args.get("replace")),
            scene_count=as_int(args.get("scene_count")),
        )
    if name == "enqueue_asset_jobs":
        from calliope.agent.capabilities.review import asset_review

        return asset_review(project_id, args)
    if name == "preview_clip_prompt":
        from calliope.agent.video_agent import preview_clip_prompt

        clip_id = as_int(args.get("clip_id"))
        if clip_id is None:
            return {"ok": False, "error": "clip_id is required"}
        return await preview_clip_prompt(project_id, clip_id, with_critic=False)
    if name == "enqueue_video_jobs":
        from calliope.agent.capabilities.review import video_review

        return video_review(project_id, args)
    if name == "summarize_canvas":
        return canvas_ops.summarize_canvas(project_id, session_id)
    if name == "create_canvas_node":
        return canvas_ops.create_canvas_node(project_id, session_id, args)
    if name == "update_canvas_node":
        return canvas_ops.update_canvas_node(project_id, session_id, args)
    if name == "delete_canvas_node":
        return canvas_ops.delete_canvas_node(project_id, session_id, args)
    if name == "canvas_connect":
        return canvas_ops.connect_nodes(project_id, session_id, args, kind="data")
    if name == "canvas_link":
        return canvas_ops.connect_nodes(project_id, session_id, args, kind="link")
    if name == "post_artifact_to_canvas":
        return await canvas_ops.post_artifact_to_canvas(project_id, session_id, args)
    if name == "run_canvas_node":
        return canvas_ops.review_node(project_id, session_id, args)
    return {"ok": False, "error": f"{name} is not a studio tool"}
