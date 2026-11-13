"""Build Scene composition. Shot tools are stored on shot_composition.scene_json."""

from __future__ import annotations

import json
from typing import Any

from calliope.agent.capabilities.sql import connect

_SHOT_NAMES = (
    "get_scene",
    "add_object",
    "delete_object",
    "rename_object",
    "set_transform",
    "reset_transform",
    "select_object",
    "clear_scene",
    "set_joint",
    "set_posture",
    "list_poses",
    "apply_pose",
    "reset_pose",
    "set_shot",
    "list_shot_presets",
    "request_capture",
    "add_keyframe",
    "update_keyframe",
    "delete_keyframe",
    "move_keyframe_time",
    "set_playback",
    "record_build_scene_gate",
    "get_build_scene_gates",
)

_PRESETS = ("wide", "medium", "closeUp", "threeQuarterLeft", "leftThird", "rightThird")


def _pose_ids() -> list[dict[str, Any]]:
    """The real pose library (calliope/poses.py + user overrides) — frontmatter only."""
    from calliope.poses import list_builtin_poses

    return [
        {
            "id": str(p["id"]),
            "name": str(p.get("name") or p["id"]),
            "category": str(p.get("category") or "authored"),
            "source": str(p.get("source") or "builtin"),
        }
        for p in list_builtin_poses()
    ]


def _pose_posture(pose: str) -> dict[str, Any] | None:
    """Full posture dict for a pose id, arity-checked (mannequin-js reads each
    entry positionally — an empty/short array NaNs the figure and it silently
    stops rendering)."""
    from calliope.poses import get_builtin_pose

    posture = get_builtin_pose(pose)
    if not isinstance(posture, dict):
        return None
    data = posture.get("data")
    if not isinstance(data, list) or not data or any(
        not isinstance(entry, list) or not entry for entry in data
    ):
        return None
    return dict(posture)


def _blank() -> dict[str, Any]:
    return {
        "objects": [],
        "shotParams": {"angle": "wide", "composition": "center"},
        "playback": {"playing": False, "elapsed": 0, "speed": 1, "duration": 8},
        "selectedObjectId": None,
        "gates": [],
    }


def load(session_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, scene_json FROM shot_composition WHERE agent_session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO shot_composition (agent_session_id, title, scene_json)
                VALUES (?, 'Scene', ?)
                """,
                (session_id, json.dumps(_blank())),
            )
            conn.commit()
            return _blank()
        try:
            data = json.loads(row["scene_json"] or "{}")
        except json.JSONDecodeError:
            data = _blank()
        if not isinstance(data, dict):
            data = _blank()
        data.setdefault("objects", [])
        data.setdefault("shotParams", {})
        data.setdefault("playback", {})
        data.setdefault("gates", [])
        return data
    finally:
        conn.close()


def save(session_id: int, data: dict[str, Any]) -> None:
    conn = connect()
    try:
        conn.execute(
            """
            UPDATE shot_composition
            SET scene_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE agent_session_id = ?
            """,
            (json.dumps(data), session_id),
        )
        conn.commit()
    finally:
        conn.close()


def _object(data: dict[str, Any], object_id: str) -> dict[str, Any] | None:
    for item in data["objects"]:
        if item.get("id") == object_id:
            return item
    return None


def execute(session_id: int | None, name: str, args: dict[str, Any]) -> dict[str, Any]:
    if session_id is None:
        return {"ok": False, "error": "shot tools need a Build Scene session"}
    data = load(session_id)
    if name == "get_scene":
        return {"ok": True, "scene": data}
    if name == "list_shot_presets":
        return {"ok": True, "presets": list(_PRESETS)}
    if name == "list_poses":
        return {"ok": True, "poses": _pose_ids()}
    if name == "get_build_scene_gates":
        return {"ok": True, "gates": data["gates"]}
    if name == "add_object":
        object_id = str(args.get("id") or f"obj-{len(data['objects']) + 1}")
        if _object(data, object_id):
            return {"ok": False, "error": f"{object_id} already exists"}
        data["objects"].append(
            {
                "id": object_id,
                "name": args.get("name") or object_id,
                "kind": args.get("kind") or "block",
                "transform": args.get("transform") or {"position": [0, 0, 0]},
                "posture": {},
                "keyframes": [],
            }
        )
        save(session_id, data)
        return {"ok": True, "id": object_id}
    object_id = str(args.get("object_id") or args.get("id") or "")
    target = _object(data, object_id) if object_id else None
    if name in {
        "delete_object",
        "rename_object",
        "set_transform",
        "reset_transform",
        "select_object",
        "set_joint",
        "set_posture",
        "apply_pose",
        "reset_pose",
        "add_keyframe",
        "update_keyframe",
        "delete_keyframe",
        "move_keyframe_time",
    } and target is None:
        return {"ok": False, "error": "object_id is required and must exist. get_scene first"}
    if name == "delete_object":
        data["objects"] = [item for item in data["objects"] if item is not target]
    elif name == "rename_object":
        target["name"] = str(args.get("name") or target["name"])
    elif name == "set_transform":
        target["transform"] = args.get("transform") or target.get("transform")
    elif name == "reset_transform":
        target["transform"] = {"position": [0, 0, 0]}
    elif name == "select_object":
        data["selectedObjectId"] = object_id
    elif name == "clear_scene":
        data["objects"] = []
        data["selectedObjectId"] = None
    elif name == "set_joint":
        joint = str(args.get("joint") or "")
        values = args.get("values")
        if not joint or not isinstance(values, list) or not values:
            return {"ok": False, "error": "joint and a non-empty values array are required"}
        target.setdefault("posture", {})[joint] = values
    elif name == "set_posture":
        posture = args.get("posture")
        if not isinstance(posture, dict):
            return {"ok": False, "error": "posture object is required"}
        target["posture"] = posture
    elif name == "apply_pose":
        pose = str(args.get("pose_id") or args.get("pose") or "")
        posture = _pose_posture(pose)
        if posture is None:
            return {
                "ok": False,
                "error": f"No pose with id '{pose}'. Call list_poses for valid ids.",
            }
        target["posture"] = posture
    elif name == "reset_pose":
        target["posture"] = {}
    elif name == "set_shot":
        data["shotParams"] = {
            "angle": args.get("angle") or data["shotParams"].get("angle"),
            "composition": args.get("composition") or data["shotParams"].get("composition"),
        }
    elif name == "request_capture":
        data["captureRequest"] = {"kind": args.get("kind") or "image"}
    elif name == "add_keyframe":
        target.setdefault("keyframes", []).append(
            {
                "id": f"kf-{len(target['keyframes']) + 1}",
                "time": args.get("time") or 0,
                "transform": target.get("transform"),
                "posture": target.get("posture"),
            }
        )
    elif name == "update_keyframe":
        keyframe_id = str(args.get("keyframe_id") or "")
        found = next((kf for kf in target["keyframes"] if kf.get("id") == keyframe_id), None)
        if found is None:
            return {"ok": False, "error": "keyframe_id was not found"}
        if args.get("transform") is not None:
            found["transform"] = args["transform"]
        if args.get("time") is not None:
            found["time"] = args["time"]
    elif name == "delete_keyframe":
        if len(target.get("keyframes") or []) <= 1:
            return {"ok": False, "error": "The last keyframe stays on the track"}
        keyframe_id = str(args.get("keyframe_id") or "")
        target["keyframes"] = [kf for kf in target["keyframes"] if kf.get("id") != keyframe_id]
    elif name == "move_keyframe_time":
        keyframe_id = str(args.get("keyframe_id") or "")
        found = next((kf for kf in target["keyframes"] if kf.get("id") == keyframe_id), None)
        if found is None:
            return {"ok": False, "error": "keyframe_id was not found"}
        found["time"] = args.get("time")
    elif name == "set_playback":
        data["playback"].update(
            {key: args[key] for key in ("playing", "elapsed", "speed", "duration") if key in args}
        )
        # The shared playhead is capped at 60s (the timeline UI and the
        # video exporter assume it).
        try:
            data["playback"]["duration"] = min(60.0, max(1.0, float(data["playback"]["duration"])))
        except (TypeError, ValueError):
            data["playback"]["duration"] = 8
    elif name == "record_build_scene_gate":
        data["gates"].append({"name": args.get("name") or "gate", "note": args.get("note") or ""})
    else:
        return {"ok": False, "error": f"{name} is not a shot tool"}
    save(session_id, data)
    return {"ok": True, "scene": data}
