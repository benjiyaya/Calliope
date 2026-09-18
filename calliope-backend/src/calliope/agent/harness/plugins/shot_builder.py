"""Shot-builder plugin: the Build Scene page's 3D blockout tools.

Mirrors open-media's mcpBridge command set as built-in harness tools (no
MCP). One composition per session (get-or-create via
routers.shots.get_or_create_for_session); every tool mutates the stored
scene_json server-side and the frontend viewport follows via the
`shot.updated` SSE event. All tools are blind_only — the Build Scene surface
belongs to unlinked sandbox sessions, exactly like sandbox canvases.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from calliope.agent.harness.registry import (
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    _db,
)
from calliope.events.bus import event_bus

# ---- posture validation (port of posture.ts v7) -----------------------------

POSTURE_ENTRIES = (
    "body.position",
    "body",
    "torso",
    "head",
    "l_leg",
    "l_knee",
    "l_ankle",
    "r_leg",
    "r_knee",
    "r_ankle",
    "l_arm",
    "l_elbow",
    "l_wrist",
    "l_finger_0",
    "l_finger_1",
    "l_finger_2",
    "l_finger_3",
    "l_finger_4",
    "r_arm",
    "r_elbow",
    "r_wrist",
    "r_finger_0",
    "r_finger_1",
    "r_finger_2",
    "r_finger_3",
    "r_finger_4",
)
POSTURE_ENTRY_LENGTHS = (
    3, 3, 3, 3, 3, 1, 3, 3, 1, 3, 3, 1, 3, 7, 7, 7, 7, 7, 3, 1, 3, 7, 7, 7, 7, 7,
)
POSTURE_VERSION = 7

JOINT_KEYS = {name for name in POSTURE_ENTRIES if name != "body.position"}

SHOT_SIZES = ("wide", "full", "medium", "mcu", "closeup")
ANGLES = ("front", "threeQuarterLeft", "threeQuarterRight", "profile", "back", "ots")
ELEVATIONS = ("eye", "high", "low")
COMPOSITIONS = (
    "center",
    "leftThird",
    "rightThird",
    "upperThird",
    "lowerThird",
    "negativeSpace",
)

# Legacy ids the agent wrote before the vocabulary matched the frontend
# solver/composition presets (which NaN'd the camera on restore: the solver
# does ANGLE_AZIMUTH_DEG[angle] and '34_left' is undefined there). Accepted
# on input, never emitted.
_ANGLE_ALIASES = {"34_left": "threeQuarterLeft", "34_right": "threeQuarterRight"}
_COMPOSITION_ALIASES = {
    "left_third": "leftThird",
    "right_third": "rightThird",
    "upper_third": "upperThird",
    "lower_third": "lowerThird",
    "negative_space": "negativeSpace",
}
OBJECT_TYPES = (
    "male",
    "female",
    "child",
    "cube",
    "plane",
    "cylinder",
    "sphere",
    "capsule",
    "cone",
    "torus",
    "camera",
)

CAPTURE_POLL_TIMEOUT_S = 10.0
CAPTURE_POLL_INTERVAL_S = 0.5


# ---- Build Scene human gates (Brief / Cut) — Phase 5 --------------------
# Persisted on scene_json.gates so they ride shot.updated SSE with the graph.
# shotStore round-trips `gates`; PATCH merges gates if a client omits them.

_GATE_NAMES = ("brief", "cut")

_BRIEF_SOFT_WARNING = (
    "brief_gate: Brief is not locked. Call ask_user with question "
    "'Lock the Build Scene brief?' and options ['Brief locked', 'Revise brief'] "
    "(lead with 'Nothing mutates until this is locked.'), then "
    "record_build_scene_gate(gate='brief') before treating edits as approved."
)

_BRIEF_SOFT_INSTRUCTION = (
    "Stop and obtain Brief via ask_user + record_build_scene_gate(gate='brief') "
    "before continuing Build Scene / shot-composer-blockout. Soft 'maybe' = hard "
    "stop. Do not call ComfyUI/workflows from this surface; export only after Cut."
)

_MUTATE_TOOLS_SOFT_BRIEF = frozenset({
    "add_object",
    "delete_object",
    "rename_object",
    "set_transform",
    "reset_transform",
    "clear_scene",
    "set_joint",
    "set_posture",
    "apply_pose",
    "reset_pose",
    "set_shot",
    "add_keyframe",
    "update_keyframe",
    "delete_keyframe",
    "move_keyframe_time",
    "set_playback",
})


def _gates_of(scene: dict[str, Any]) -> dict[str, Any]:
    raw = scene.get("gates")
    return raw if isinstance(raw, dict) else {}


def _public_gates(scene: dict[str, Any]) -> dict[str, Any]:
    """Normalize gates for tool / API responses."""
    out: dict[str, Any] = {"brief": None, "cut": None}
    for name in _GATE_NAMES:
        entry = _gates_of(scene).get(name)
        if isinstance(entry, dict) and entry.get("approved_at"):
            out[name] = {
                "approved_at": entry.get("approved_at"),
                "source": entry.get("source") or "agent",
                "note": entry.get("note"),
            }
    return out


def _gate_approved(scene: dict[str, Any], gate: str) -> bool:
    entry = _gates_of(scene).get(gate)
    return isinstance(entry, dict) and bool(entry.get("approved_at"))


def _brief_soft_fields(scene: dict[str, Any]) -> dict[str, Any]:
    if _gate_approved(scene, "brief"):
        return {}
    return {
        "warning": _BRIEF_SOFT_WARNING,
        "instruction": _BRIEF_SOFT_INSTRUCTION,
        "gates": _public_gates(scene),
    }


def _wrap_brief_soft(executor):  # type: ignore[no-untyped-def]
    """Soft-warn mutating shot_* tools when Brief is not recorded."""

    async def wrapped(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        _sid, scene_before = _load_scene(ctx)
        result = await executor(ctx, args)
        if isinstance(result, dict) and result.get("ok"):
            extra = _brief_soft_fields(scene_before)
            if extra:
                result = {**result, **extra}
        return result

    return wrapped


def _scene_of(comp: dict[str, Any]) -> dict[str, Any]:
    try:
        scene = json.loads(comp.get("scene_json") or "{}")
    except json.JSONDecodeError:
        scene = {}
    return scene if isinstance(scene, dict) else {}


def _find_object(scene: dict[str, Any], object_id: str) -> dict[str, Any] | None:
    for obj in scene.get("objects", []):
        if isinstance(obj, dict) and obj.get("id") == object_id:
            return obj
    return None


def _summarize_object(obj: dict[str, Any]) -> dict[str, Any]:
    """Wire-format for the model — mirrors open-media's summarizeObject."""
    return {
        "id": obj.get("id"),
        "name": obj.get("name"),
        "type": obj.get("type"),
        "visible": obj.get("visible", True),
        "locked": obj.get("locked", False),
        "transform": obj.get("transform"),
        "fov": obj.get("fov"),
        "posture": obj.get("posture"),
        "hasDefaultPosture": bool(obj.get("defaultPosture")),
        "keyframes": [
            {
                "id": k.get("id"),
                "time": k.get("time"),
                "transform": k.get("transform"),
                "fov": k.get("fov"),
                "hasPosture": bool(k.get("posture")),
            }
            for k in (obj.get("keyframes") or [])
            if isinstance(k, dict)
        ],
    }


def _save_scene(ctx: ToolContext, scene: dict[str, Any]) -> dict[str, Any]:
    from calliope.routers.shots import get_or_create_for_session, validate_scene_json

    raw = json.dumps(scene)
    validate_scene_json(raw)
    conn = _db()
    try:
        comp = get_or_create_for_session(conn, ctx.session_id)
        comp_id = int(comp["id"])
        conn.execute(
            "UPDATE shot_composition SET scene_json = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (raw, comp_id),
        )
        conn.commit()
    finally:
        conn.close()
    return comp_id


async def _publish_updated(shot_id: int, reason: str) -> None:
    await event_bus.publish("shot.updated", {"shot_id": shot_id, "reason": reason})


def _ok(shot_id: int, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "shot_id": shot_id, **extra}


def _load_scene(ctx: ToolContext) -> tuple[int, dict[str, Any]]:
    from calliope.routers.shots import get_or_create_for_session

    conn = _db()
    try:
        comp = get_or_create_for_session(conn, ctx.session_id)
    finally:
        conn.close()
    return int(comp["id"]), _scene_of(comp)


def _validate_posture(posture: Any) -> str | None:
    """Returns an error string, or None when the posture is well-formed."""
    if not isinstance(posture, dict):
        return "posture must be an object"
    version = posture.get("version")
    if version != POSTURE_VERSION:
        return f"posture.version must be {POSTURE_VERSION}"
    data = posture.get("data")
    if not isinstance(data, list) or len(data) != len(POSTURE_ENTRIES):
        return (
            f"posture.data must be a list of {len(POSTURE_ENTRIES)} entries "
            "(one per joint, in POSTURE_ENTRIES order)"
        )
    for i, entry in enumerate(data):
        expected = POSTURE_ENTRY_LENGTHS[i]
        if not isinstance(entry, list) or len(entry) != expected or not all(
            isinstance(v, (int, float)) for v in entry
        ):
            name = POSTURE_ENTRIES[i]
            return (
                f"posture.data[{i}] ({name}) must be a list of {expected} numbers"
            )
    return None


def _apply_joint(
    obj: dict[str, Any], joint: str, values: list[float]
) -> None:
    """Write one joint into the object's posture, initializing when absent.

    Fresh entries are zeroed at their real lengths, never empty arrays:
    mannequin-js reads each entry with fixed-arity accessors (e.g.
    ``rotation.set(rad(pos[0]), ...)``), so an empty array becomes NaN and the
    whole figure silently stops rendering.
    """
    posture = obj.get("posture")
    if (
        not isinstance(posture, dict)
        or posture.get("version") != POSTURE_VERSION
        or not isinstance(posture.get("data"), list)
        or len(posture["data"]) != len(POSTURE_ENTRIES)
    ):
        posture = {
            "version": POSTURE_VERSION,
            "data": [[0.0] * length for length in POSTURE_ENTRY_LENGTHS],
        }
    data = posture["data"]
    idx = POSTURE_ENTRIES.index(joint)
    data[idx] = [float(v) for v in values]
    obj["posture"] = posture


# ---- tool executors ---------------------------------------------------------


async def t_get_scene(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    shot_id, scene = _load_scene(ctx)
    return _ok(
        shot_id,
        objects=[_summarize_object(o) for o in scene.get("objects", []) if isinstance(o, dict)],
        shotParams=scene.get("shotParams"),
        playback=scene.get("playback"),
        gates=_public_gates(scene),
    )


async def t_add_object(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    import uuid as _uuid

    otype = args.get("type")
    if otype not in OBJECT_TYPES:
        return {"ok": False, "error": f"type must be one of: {', '.join(OBJECT_TYPES)}"}
    shot_id, scene = _load_scene(ctx)
    objects = scene.setdefault("objects", [])
    name = (args.get("name") or "").strip() or f"{otype.capitalize()} {len(objects) + 1:02d}"
    transform = {
        "position": [len(objects) * 1.5, 0, 0],
        "rotation": [0, 0, 0],
        "scale": [1, 1, 1],
    }
    obj = {
        "id": str(_uuid.uuid4()),
        "name": name,
        "type": otype,
        "visible": True,
        "locked": False,
        "transform": transform,
        "keyframes": [] if otype == "camera" else [
            {"id": str(_uuid.uuid4()), "time": 0, "transform": transform}
        ],
        "liveEditTime": None,
    }
    if otype == "camera":
        obj["fov"] = 10
    objects.append(obj)
    comp_id = _save_scene(ctx, scene)
    await _publish_updated(comp_id, "object_added")
    return _ok(comp_id, object=_summarize_object(obj))


async def t_delete_object(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    object_id = args.get("object_id")
    shot_id, scene = _load_scene(ctx)
    objects = scene.get("objects", [])
    remaining = [o for o in objects if isinstance(o, dict) and o.get("id") != object_id]
    if len(remaining) == len(objects):
        return {"ok": False, "error": f"No object with id '{object_id}' in this composition."}
    scene["objects"] = remaining
    comp_id = _save_scene(ctx, scene)
    await _publish_updated(comp_id, "object_deleted")
    return _ok(comp_id)


async def t_rename_object(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    object_id = args.get("object_id")
    name = (args.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "name is required"}
    shot_id, scene = _load_scene(ctx)
    obj = _find_object(scene, object_id)
    if obj is None:
        return {"ok": False, "error": f"No object with id '{object_id}' in this composition."}
    obj["name"] = name[:120]
    comp_id = _save_scene(ctx, scene)
    await _publish_updated(comp_id, "object_renamed")
    return _ok(comp_id, object=_summarize_object(obj))


async def t_set_transform(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    object_id = args.get("object_id")
    shot_id, scene = _load_scene(ctx)
    obj = _find_object(scene, object_id)
    if obj is None:
        return {"ok": False, "error": f"No object with id '{object_id}' in this composition."}
    transform_in = args.get("transform") or {}
    base = obj.get("transform") or {
        "position": [0, 0, 0],
        "rotation": [0, 0, 0],
        "scale": [1, 1, 1],
    }
    merged = dict(base)
    for key in ("position", "rotation", "scale"):
        if key in transform_in:
            vals = transform_in[key]
            if not isinstance(vals, list) or len(vals) != 3 or not all(
                isinstance(v, (int, float)) for v in vals
            ):
                return {"ok": False, "error": f"transform.{key} must be [x, y, z] numbers"}
            merged[key] = [float(v) for v in vals]
    obj["transform"] = merged
    obj["liveEditTime"] = None
    # A track-less object keeps its spawn keyframe in sync with the plain
    # fields. An object WITH committed keyframes must not be touched here:
    # force-rewriting keyframes[0] snapped every earlier keyframe to the new
    # position (a keyed walk-across teleported at t=0) and broke the
    # stage→add_keyframe recipe — the keyframe tools own the track.
    if not obj.get("keyframes"):
        obj["keyframes"] = [{"id": f"kf-{uuid.uuid4().hex[:12]}", "time": 0, "transform": merged}]
    comp_id = _save_scene(ctx, scene)
    await _publish_updated(comp_id, "transform_set")
    return _ok(comp_id, object=_summarize_object(obj))


async def t_reset_transform(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    args = dict(args)
    args["transform"] = {"position": [0, 0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1]}
    return await t_set_transform(ctx, args)


async def t_select_object(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    # Selection is a UI-only concern; the model uses it to point at an object
    # so the user sees which figure is being discussed.
    object_id = args.get("object_id")
    shot_id, scene = _load_scene(ctx)
    obj = _find_object(scene, object_id)
    if obj is None:
        return {"ok": False, "error": f"No object with id '{object_id}' in this composition."}
    scene["selectedObjectId"] = object_id
    comp_id = _save_scene(ctx, scene)
    await _publish_updated(comp_id, "selection")
    return _ok(comp_id, selected_object=_summarize_object(obj))


async def t_clear_scene(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    shot_id, scene = _load_scene(ctx)
    # Keep Brief/Cut approvals across wipe — gates live beside objects on scene_json.
    next_scene: dict[str, Any] = {"objects": []}
    gates = scene.get("gates")
    if isinstance(gates, dict):
        next_scene["gates"] = gates
    comp_id = _save_scene(ctx, next_scene)
    await _publish_updated(comp_id, "cleared")
    return _ok(comp_id, gates=_public_gates(next_scene))


# ── keyframes / motion (port of open-media's mcpBridge keyframe commands) ──

_KEYFRAME_TIME_MAX = 60.0  # matches the frontend MAX_TIMELINE_DURATION cap


def _validate_keyframe_time(time: Any) -> str | None:
    if not isinstance(time, (int, float)) or isinstance(time, bool):
        return "time must be a number (seconds)"
    if time < 0 or time > _KEYFRAME_TIME_MAX:
        return f"time must be between 0 and {_KEYFRAME_TIME_MAX:g} seconds"
    return None


async def t_add_keyframe(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Upserts a keyframe on the object's track at `time`.

    Seeds transform/posture/fov from the object's current state, so the common
    agent flow is: set_transform → add_keyframe(t) → set_transform →
    add_keyframe(t2) — each keyframe captures what the object looks like now.
    An existing keyframe within one frame (1/24s) of `time` is replaced.
    """
    object_id = args.get("object_id")
    time = args.get("time")
    if (err := _validate_keyframe_time(time)) is not None:
        return {"ok": False, "error": err}
    shot_id, scene = _load_scene(ctx)
    obj = _find_object(scene, object_id)
    if obj is None:
        return {"ok": False, "error": f"No object with id '{object_id}' in this composition."}

    transform = obj.get("transform") or {
        "position": [0, 0, 0],
        "rotation": [0, 0, 0],
        "scale": [1, 1, 1],
    }
    keyframes = [k for k in (obj.get("keyframes") or []) if isinstance(k, dict)]
    near = next(
        (k for k in keyframes if abs(float(k.get("time", -1)) - float(time)) <= 1 / 24),
        None,
    )
    if near is not None:
        near["time"] = float(time)
        near["transform"] = transform
        if obj.get("posture") is not None:
            near["posture"] = obj["posture"]
        if obj.get("fov") is not None:
            near["fov"] = obj["fov"]
    else:
        keyframe: dict[str, Any] = {
            "id": f"kf-{uuid.uuid4().hex[:12]}",
            "time": float(time),
            "transform": transform,
        }
        if obj.get("posture") is not None:
            keyframe["posture"] = obj["posture"]
        if obj.get("fov") is not None:
            keyframe["fov"] = obj["fov"]
        keyframes.append(keyframe)
    obj["keyframes"] = sorted(keyframes, key=lambda k: float(k.get("time", 0)))
    scene["playback"] = {**(scene.get("playback") or {}), "duration": max(
        float((scene.get("playback") or {}).get("duration") or 6), float(time)
    )}
    comp_id = _save_scene(ctx, scene)
    await _publish_updated(comp_id, "keyframe_added")
    return _ok(comp_id, object=_summarize_object(obj))


async def t_update_keyframe(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    object_id = args.get("object_id")
    keyframe_id = args.get("keyframe_id")
    shot_id, scene = _load_scene(ctx)
    obj = _find_object(scene, object_id)
    if obj is None:
        return {"ok": False, "error": f"No object with id '{object_id}' in this composition."}
    keyframe = next((k for k in obj.get("keyframes") or [] if k.get("id") == keyframe_id), None)
    if keyframe is None:
        return {"ok": False, "error": f"No keyframe '{keyframe_id}' on object '{object_id}'. Call get_scene to see keyframe ids."}
    transform_in = args.get("transform") or {}
    base = keyframe.get("transform") or {
        "position": [0, 0, 0],
        "rotation": [0, 0, 0],
        "scale": [1, 1, 1],
    }
    merged = dict(base)
    for key in ("position", "rotation", "scale"):
        if key in transform_in:
            vals = transform_in[key]
            if not isinstance(vals, list) or len(vals) != 3 or not all(
                isinstance(v, (int, float)) for v in vals
            ):
                return {"ok": False, "error": f"transform.{key} must be [x, y, z] numbers"}
            merged[key] = [float(v) for v in vals]
    keyframe["transform"] = merged
    comp_id = _save_scene(ctx, scene)
    await _publish_updated(comp_id, "keyframe_updated")
    return _ok(comp_id, object=_summarize_object(obj))


async def t_delete_keyframe(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    object_id = args.get("object_id")
    keyframe_id = args.get("keyframe_id")
    shot_id, scene = _load_scene(ctx)
    obj = _find_object(scene, object_id)
    if obj is None:
        return {"ok": False, "error": f"No object with id '{object_id}' in this composition."}
    keyframes = [k for k in (obj.get("keyframes") or []) if isinstance(k, dict)]
    if len(keyframes) <= 1:
        return {"ok": False, "error": "Refusing to delete the last keyframe — a track keeps at least one anchor."}
    remaining = [k for k in keyframes if k.get("id") != keyframe_id]
    if len(remaining) == len(keyframes):
        return {"ok": False, "error": f"No keyframe '{keyframe_id}' on object '{object_id}'."}
    obj["keyframes"] = remaining
    comp_id = _save_scene(ctx, scene)
    await _publish_updated(comp_id, "keyframe_deleted")
    return _ok(comp_id, object=_summarize_object(obj))


async def t_move_keyframe_time(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    object_id = args.get("object_id")
    keyframe_id = args.get("keyframe_id")
    time = args.get("time")
    if (err := _validate_keyframe_time(time)) is not None:
        return {"ok": False, "error": err}
    shot_id, scene = _load_scene(ctx)
    obj = _find_object(scene, object_id)
    if obj is None:
        return {"ok": False, "error": f"No object with id '{object_id}' in this composition."}
    keyframe = next((k for k in obj.get("keyframes") or [] if k.get("id") == keyframe_id), None)
    if keyframe is None:
        return {"ok": False, "error": f"No keyframe '{keyframe_id}' on object '{object_id}'."}
    keyframe["time"] = float(time)
    obj["keyframes"] = sorted(
        (k for k in obj.get("keyframes") or [] if isinstance(k, dict)),
        key=lambda k: float(k.get("time", 0)),
    )
    comp_id = _save_scene(ctx, scene)
    await _publish_updated(comp_id, "keyframe_moved")
    return _ok(comp_id, object=_summarize_object(obj))


async def t_set_playback(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Controls the playhead the UI samples object tracks and the camera track at."""
    shot_id, scene = _load_scene(ctx)
    playback = scene.get("playback") or {}
    if "playing" in args:
        playback["playing"] = bool(args["playing"])
    if "elapsed" in args:
        elapsed = args["elapsed"]
        if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool) or elapsed < 0:
            return {"ok": False, "error": "elapsed must be a non-negative number (seconds)"}
        playback["elapsed"] = float(elapsed)
    if "speed" in args:
        speed = args["speed"]
        if not isinstance(speed, (int, float)) or isinstance(speed, bool) or speed <= 0:
            return {"ok": False, "error": "speed must be a positive number"}
        playback["speed"] = float(speed)
    if "duration" in args:
        duration = args["duration"]
        if (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or duration < 1
            or duration > _KEYFRAME_TIME_MAX
        ):
            return {"ok": False, "error": f"duration must be between 1 and {_KEYFRAME_TIME_MAX:g} seconds"}
        playback["duration"] = float(duration)
    scene["playback"] = playback
    comp_id = _save_scene(ctx, scene)
    await _publish_updated(comp_id, "playback_set")
    return _ok(comp_id, playback=playback)


async def t_set_joint(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    object_id = args.get("object_id")
    joint = args.get("joint")
    if joint not in JOINT_KEYS:
        return {"ok": False, "error": f"joint must be one of: {', '.join(sorted(JOINT_KEYS))}"}
    values = args.get("values")
    expected = POSTURE_ENTRY_LENGTHS[POSTURE_ENTRIES.index(joint)]
    if not isinstance(values, list) or len(values) != expected or not all(
        isinstance(v, (int, float)) for v in values
    ):
        return {
            "ok": False,
            "error": f"values for '{joint}' must be a list of {expected} numbers "
            "(degrees; euler xyz for most joints, a single bend for knees/elbows, "
            "7 for fingers)",
        }
    shot_id, scene = _load_scene(ctx)
    obj = _find_object(scene, object_id)
    if obj is None:
        return {"ok": False, "error": f"No object with id '{object_id}' in this composition."}
    if obj.get("type") in ("camera",) or obj.get("type", "").startswith(
        ("cube", "plane", "cylinder", "sphere", "capsule", "cone", "torus")
    ):
        return {"ok": False, "error": f"Object '{obj.get('name')}' has no joints to pose."}
    _apply_joint(obj, joint, values)
    comp_id = _save_scene(ctx, scene)
    await _publish_updated(comp_id, "joint_set")
    return _ok(comp_id, object=_summarize_object(obj))


async def t_set_posture(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    object_id = args.get("object_id")
    shot_id, scene = _load_scene(ctx)
    obj = _find_object(scene, object_id)
    if obj is None:
        return {"ok": False, "error": f"No object with id '{object_id}' in this composition."}
    posture = args.get("posture")
    error = _validate_posture(posture)
    if error:
        return {"ok": False, "error": f"Invalid posture: {error}"}
    obj["posture"] = posture
    comp_id = _save_scene(ctx, scene)
    await _publish_updated(comp_id, "posture_set")
    return _ok(comp_id, object=_summarize_object(obj))


async def t_list_poses(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from calliope.poses import list_builtin_poses

    return _ok(0, poses=list_builtin_poses())


async def t_apply_pose(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from calliope.poses import get_builtin_pose

    object_id = args.get("object_id")
    pose_id = args.get("pose_id")
    shot_id, scene = _load_scene(ctx)
    obj = _find_object(scene, object_id)
    if obj is None:
        return {"ok": False, "error": f"No object with id '{object_id}' in this composition."}
    posture = get_builtin_pose(pose_id)
    if posture is None:
        return {"ok": False, "error": f"No pose with id '{pose_id}'. Call list_poses for valid ids."}
    obj["posture"] = posture
    comp_id = _save_scene(ctx, scene)
    await _publish_updated(comp_id, "pose_applied")
    return _ok(comp_id, object=_summarize_object(obj))


async def t_reset_pose(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    object_id = args.get("object_id")
    shot_id, scene = _load_scene(ctx)
    obj = _find_object(scene, object_id)
    if obj is None:
        return {"ok": False, "error": f"No object with id '{object_id}' in this composition."}
    obj.pop("posture", None)
    comp_id = _save_scene(ctx, scene)
    await _publish_updated(comp_id, "pose_reset")
    return _ok(comp_id, object=_summarize_object(obj))


async def t_set_shot(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    shot_id, scene = _load_scene(ctx)
    params = scene.get("shotParams") or {}
    for key, allowed, aliases in (
        ("shotSize", SHOT_SIZES, {}),
        ("angle", ANGLES, _ANGLE_ALIASES),
        ("elevation", ELEVATIONS, {}),
        ("composition", COMPOSITIONS, _COMPOSITION_ALIASES),
    ):
        value = args.get(key)
        if value is None:
            continue
        if aliases and value in aliases:
            value = aliases[value]
        if value not in allowed:
            return {
                "ok": False,
                "error": f"{key} must be one of: {', '.join(allowed)}",
            }
        params[key] = value
    scene["shotParams"] = params
    comp_id = _save_scene(ctx, scene)
    await _publish_updated(comp_id, "shot_set")
    return _ok(comp_id, shotParams=params)


async def t_list_shot_presets(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return _ok(
        0,
        shotSize=list(SHOT_SIZES),
        angle=list(ANGLES),
        elevation=list(ELEVATIONS),
        composition=list(COMPOSITIONS),
    )


async def t_request_capture(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Capture a still via the UI Three.js viewport.

    Stamps capture_request_json and polls for the viewport to POST the PNG
    (the Build Scene page renders the canvas and uploads the data URL). This is
    the only capture path — stills come from the 3D editor, not a headless
    renderer.
    """
    from calliope.routers.shots import get_or_create_for_session

    label = (args.get("label") or "capture").strip()[:200]
    conn = _db()
    try:
        comp = get_or_create_for_session(conn, ctx.session_id)
        comp_id = int(comp["id"])
    finally:
        conn.close()

    # --- UI poll path -----------------------------------------------------
    conn = _db()
    try:
        conn.execute(
            "UPDATE shot_composition SET capture_request_json = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (json.dumps({"label": label, "requested_at": time.time()}), comp_id),
        )
        conn.commit()
    finally:
        conn.close()
    await _publish_updated(comp_id, "capture_requested")

    deadline = time.monotonic() + CAPTURE_POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        await asyncio.sleep(CAPTURE_POLL_INTERVAL_S)
        conn = _db()
        try:
            row = conn.execute(
                "SELECT capture_request_json FROM shot_composition WHERE id = ?",
                (comp_id,),
            ).fetchone()
            if row is not None and not row["capture_request_json"]:
                cap = conn.execute(
                    "SELECT id, file_path, label FROM shot_capture "
                    "WHERE composition_id = ? ORDER BY id DESC LIMIT 1",
                    (comp_id,),
                ).fetchone()
                return _ok(
                    comp_id,
                    label=label,
                    capture_id=cap["id"] if cap else None,
                    file_path=cap["file_path"] if cap else None,
                    note="Capture saved via the 3D editor viewport. Reference it "
                    "in generation by this path.",
                )
        finally:
            conn.close()
    return {
        "ok": False,
        "shot_id": comp_id,
        "capture_pending": True,
        "error": (
            "Capture request timed out after "
            f"{CAPTURE_POLL_TIMEOUT_S:.0f}s — the Build Scene page did not "
            "save the PNG (it is probably closed). Tell the user to open "
            "the Build Scene page and re-run capture; do NOT reference a "
            "file path."
        ),
    }




async def t_record_build_scene_gate(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Record Brief or Cut approval after ask_user affirmation (shot-composer-blockout)."""
    from datetime import datetime, timezone

    gate = str(args.get("gate") or "").strip().lower()
    if gate not in _GATE_NAMES:
        return {"ok": False, "error": "gate must be 'brief' or 'cut'"}
    note = args.get("note")
    if note is not None:
        note = str(note).strip()[:500] or None

    shot_id, scene = _load_scene(ctx)
    gates = dict(_gates_of(scene))
    gates[gate] = {
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "source": "agent",
        "note": note,
    }
    scene["gates"] = gates
    comp_id = _save_scene(ctx, scene)
    await _publish_updated(comp_id, f"gate_{gate}")
    return _ok(comp_id, gate=gate, gates=_public_gates(scene))


async def t_get_build_scene_gates(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    shot_id, scene = _load_scene(ctx)
    return _ok(shot_id, gates=_public_gates(scene))


# ---- registration -----------------------------------------------------------


def register(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="get_scene",
            description=(
                "See the current 3D composition (Build Scene): every object's "
                "id/name/type/transform/posture plus shot framing. Call before "
                "any edit."
            ),
            parameters={"type": "object", "properties": {}},
            executor=t_get_scene,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="add_object",
            description=(
                "Add an object to the 3D composition: a character "
                "('male'/'female'/'child'), a primitive blockout shape "
                "(cube/plane/cylinder/sphere/capsule/cone/torus), or a camera. "
                "Returns the new object id."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": list(OBJECT_TYPES),
                    },
                    "name": {"type": "string", "description": "Optional display name"},
                },
                "required": ["type"],
            },
            executor=t_add_object,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="delete_object",
            description="Remove one object from the 3D composition.",
            parameters={
                "type": "object",
                "properties": {"object_id": {"type": "string"}},
                "required": ["object_id"],
            },
            executor=t_delete_object,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="rename_object",
            description="Rename an object in the 3D composition.",
            parameters={
                "type": "object",
                "properties": {
                    "object_id": {"type": "string"},
                    "name": {"type": "string"},
                },
                "required": ["object_id", "name"],
            },
            executor=t_rename_object,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="set_transform",
            description=(
                "Move/rotate/scale an object. Partial — only the keys you send "
                "change. position/rotation/scale are [x, y, z]; rotation is "
                "degrees. y is up. Characters stand on y=0."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "object_id": {"type": "string"},
                    "transform": {
                        "type": "object",
                        "properties": {
                            "position": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 3,
                                "maxItems": 3,
                            },
                            "rotation": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 3,
                                "maxItems": 3,
                            },
                            "scale": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 3,
                                "maxItems": 3,
                            },
                        },
                    },
                },
                "required": ["object_id"],
            },
            executor=t_set_transform,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="reset_transform",
            description="Reset an object to the origin with identity rotation/scale.",
            parameters={
                "type": "object",
                "properties": {"object_id": {"type": "string"}},
                "required": ["object_id"],
            },
            executor=t_reset_transform,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="select_object",
            description=(
                "Highlight an object for the user (UI selection). Use to point "
                "at what you are talking about, not required before edits."
            ),
            parameters={
                "type": "object",
                "properties": {"object_id": {"type": "string"}},
                "required": ["object_id"],
            },
            executor=t_select_object,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="clear_scene",
            description="Remove every object from the 3D composition (start over).",
            parameters={"type": "object", "properties": {}},
            executor=t_clear_scene,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="set_joint",
            description=(
                "Pose one joint of a character (degrees) for pose/gizmo editing "
                "or sparse motion-recipe keys — NOT per-frame puppeting in "
                "Build Scene. Joints: torso, head, l_leg/r_leg, "
                "l_knee/r_knee (1 bend), l_ankle/r_ankle, l_arm/r_arm, "
                "l_elbow/r_elbow (1 bend), l_wrist/r_wrist, "
                "l_finger_0..4 / r_finger_0..4 (7 values). Most joints take "
                "3 euler values [x, y, z]. Prefer apply_pose + recipes over "
                "frame-by-frame set_joint loops."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "object_id": {"type": "string"},
                    "joint": {"type": "string"},
                    "values": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "3 euler values, 1 for knees/elbows, 7 for fingers",
                    },
                },
                "required": ["object_id", "joint", "values"],
            },
            executor=t_set_joint,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="set_posture",
            description=(
                "Advanced: replace a character's whole posture (the serialized "
                "v7 posture object). Prefer set_joint for ordinary posing."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "object_id": {"type": "string"},
                    "posture": {"type": "object"},
                },
                "required": ["object_id", "posture"],
            },
            executor=t_set_posture,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="list_poses",
            description="List built-in poses that apply_pose can put on a character.",
            parameters={"type": "object", "properties": {}},
            executor=t_list_poses,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="apply_pose",
            description="Apply a built-in pose (id from list_poses) to a character.",
            parameters={
                "type": "object",
                "properties": {
                    "object_id": {"type": "string"},
                    "pose_id": {"type": "string"},
                },
                "required": ["object_id", "pose_id"],
            },
            executor=t_apply_pose,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="reset_pose",
            description="Clear a character's pose back to the default stance.",
            parameters={
                "type": "object",
                "properties": {"object_id": {"type": "string"}},
                "required": ["object_id"],
            },
            executor=t_reset_pose,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="set_shot",
            description=(
                "Frame the shot: shotSize (wide/full/medium/mcu/closeup), angle "
                "(front/threeQuarterLeft/threeQuarterRight/profile/back/ots), "
                "elevation (eye/high/low), composition (center/leftThird/"
                "rightThird/upperThird/lowerThird/negativeSpace). Partial — "
                "only the keys you send change. OTS needs a second character."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "shotSize": {"type": "string", "enum": list(SHOT_SIZES)},
                    "angle": {"type": "string", "enum": list(ANGLES)},
                    "elevation": {"type": "string", "enum": list(ELEVATIONS)},
                    "composition": {"type": "string", "enum": list(COMPOSITIONS)},
                },
            },
            executor=t_set_shot,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="list_shot_presets",
            description="List valid shot-size/angle/elevation/composition ids for set_shot.",
            parameters={"type": "object", "properties": {}},
            executor=t_list_shot_presets,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="request_capture",
            description=(
                "Render the current viewport as a PNG reference (for ControlNet "
                "or prompt reference). The Build Scene page saves it; returns "
                "the file path to reference in generation. FAILS if the Build "
                "Scene page is not open — ask the user to open it before "
                "calling, and never reference a file path on failure."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Short capture label"}
                },
            },
            executor=t_request_capture,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="add_keyframe",
            description=(
                "Pin the object's CURRENT transform/posture as a keyframe at "
                "`time` (seconds, 0-60). Workflow: set_transform the object, "
                "add_keyframe, set_transform again, add_keyframe at a later "
                "time — playback then glides between them. Replaces a "
                "keyframe within 1/24s of `time`. Camera objects excluded "
                "(keyframe the camera via the UI timeline instead)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "object_id": {"type": "string"},
                    "time": {"type": "number", "description": "Seconds (0-60)"},
                },
                "required": ["object_id", "time"],
            },
            executor=t_add_keyframe,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="update_keyframe",
            description=(
                "Change an existing keyframe's transform (partial position/"
                "rotation/scale). Get keyframe ids from get_scene."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "object_id": {"type": "string"},
                    "keyframe_id": {"type": "string"},
                    "transform": {
                        "type": "object",
                        "properties": {
                            "position": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                            "rotation": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                            "scale": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                        },
                    },
                },
                "required": ["object_id", "keyframe_id", "transform"],
            },
            executor=t_update_keyframe,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="delete_keyframe",
            description="Remove one keyframe from an object's track (the last remaining keyframe is kept).",
            parameters={
                "type": "object",
                "properties": {
                    "object_id": {"type": "string"},
                    "keyframe_id": {"type": "string"},
                },
                "required": ["object_id", "keyframe_id"],
            },
            executor=t_delete_keyframe,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="move_keyframe_time",
            description="Move an existing keyframe to a new time (seconds, 0-60) without changing what it stores.",
            parameters={
                "type": "object",
                "properties": {
                    "object_id": {"type": "string"},
                    "keyframe_id": {"type": "string"},
                    "time": {"type": "number", "description": "Seconds (0-60)"},
                },
                "required": ["object_id", "keyframe_id", "time"],
            },
            executor=t_move_keyframe_time,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="set_playback",
            description=(
                "Control the timeline: play/pause, scrub the playhead "
                "(elapsed seconds), speed, or duration (1-60s). Playback "
                "samples every tracked object's keyframes and the camera "
                "track; export renders the camera track."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "playing": {"type": "boolean"},
                    "elapsed": {"type": "number", "description": "Seconds"},
                    "speed": {"type": "number"},
                    "duration": {"type": "number", "description": "Seconds (1-60)"},
                },
            },
            executor=t_set_playback,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )

    registry.register(
        ToolDefinition(
            name="record_build_scene_gate",
            description=(
                "Record Brief or Cut approval for Build Scene (shot-composer-blockout). "
                "Call ONLY after ask_user affirmation — Brief before mutates, "
                "Cut before export. Soft 'maybe' is not approval."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "gate": {
                        "type": "string",
                        "enum": ["brief", "cut"],
                        "description": "Which human gate was approved",
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional short note (e.g. brief summary)",
                    },
                },
                "required": ["gate"],
            },
            executor=t_record_build_scene_gate,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="get_build_scene_gates",
            description=(
                "Return Brief/Cut gate state for the session composition "
                "(also included on get_scene as `gates`)."
            ),
            parameters={"type": "object", "properties": {}},
            executor=t_get_build_scene_gates,
            category="shot",
            requires_project=False,
            blind_only=True,
        )
    )

    # Soft Brief warning on mutators (export hard-enforces Cut separately).
    for _name in _MUTATE_TOOLS_SOFT_BRIEF:
        _defn = registry.get(_name)
        if _defn is not None:
            _defn.executor = _wrap_brief_soft(_defn.executor)
