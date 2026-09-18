---
name: shot-composer-blockout
description: "Use when the user asks to build, pose, frame, or ANIMATE a 3D scene in Build Scene — characters, primitives, shot framing, keyframe motion, or exporting a blockout as a generation reference."
version: 1.0.2
license: MIT
metadata:
  author: Calliope
  tags: [build-scene, 3d, blockout, keyframes, camera, controlnet]
---

# Shot Composer Blockout

## Overview

Judgment layer for driving the Build Scene composition through the `shot_*`
tools. The machine contract lives in the tools themselves; this skill is the
workflow — how to think about the scene, and in what order to touch it.
Adapted from open-media's agent guide, with motion-recipe judgment folded in.

## Scene model

- The scene is a flat list of **objects**: characters (`male`/`female`/`child`),
  primitives (`cube`, `plane`, `cylinder`, `sphere`, `capsule`, `cone`,
  `torus`). `get_scene` first, always — it's cheap and tells you what exists,
  what's selected, playback state, and every keyframe id.
- Objects are addressed by **id** from `get_scene`/`add_object` — never invent
  ids. `select_object` is for pointing the user's UI at the subject (and
  `set_shot` solves from it); edits themselves always pass explicit ids.
- **Posture is opaque** (`{version, data:[...]}`, fixed-arity arrays). Never
  hand-construct it — pose via `list_poses` + `apply_pose`, then fine-tune
  single joints with `set_joint`.
- Framing (`shotSize/angle/elevation/composition`) is scene-global, not
  per-object. `set_shot` merges partial updates. OTS needs a second character.

## Preferred workflow

1. `get_scene` — see what's there.
2. Build: `add_object`, `set_transform` ([x,y,z], y-up, characters stand on
   y=0), `apply_pose` for stances.
3. Frame: `select_object` the subject → `set_shot` (montage via
   `list_shot_presets` when unsure of valid ids).
4. Animate: stage → `add_keyframe` → stage → `add_keyframe` (see
   `references/motion-recipes.md` — read it before any non-trivial motion).
5. `request_capture` when the user wants the frame as a generation reference.
   It returns the saved PNG path — no GPU, no approval.

## Motion rules (read before keyframing)

- **Stage, then commit.** `set_transform`/`set_joint` write the object's live
  state; `add_keyframe(object_id, time)` pins that state at a second (0-60).
  An object with no track gets a spawn keyframe at t=0 automatically.
- **Never call `add_keyframe` twice with the same time for different poses** —
  within 1/24s it REPLACES (upsert). Move it with `move_keyframe_time` instead.
- **Respect the track.** Once a track exists, `set_transform` only stages — it
  does NOT rewrite committed keyframes. To change the pose AT a keyframe, edit
  with `update_keyframe`, not `set_transform`.
- **Duration ≤ 60s.** `set_playback(duration)` caps there; keep keyframe times
  within the duration. Preview loops.


## Camera, gates, and export

Build Scene has a **single** surface: the Three.js viewport. It is the only
preview and the only export/capture source — there is no separate render path
and no Remotion.

- **Camera framing is the user's job.** The viewport camera is keyed from the
  TimelineStrip (`◉`). Do not drive the camera from `shot_*` tools, and do not
  invent camera-beat tools — none exist.
- **Object motion is your job.** Stage → `add_keyframe` recipes animate objects;
  that motion is what the exported clip renders (the viewport samples every
  tracked object at the playhead, so object keyframes appear in the export even
  with a static camera).
- **Brief/Cut gates.** Before mutating, `ask_user` the Brief; before export,
  `ask_user` the Cut and `record_build_scene_gate(gate='cut')`. Soft maybe =
  hard stop. The user clicks **Export video** — there is no `export_video`
  tool.
- **ComfyUI / workflows** only when the user explicitly asks image/video gen,
  or for stills that need Comfy after Capture — never as part of the Build
  Scene loop.

## When a capability seems missing

Don't work around a gap by inventing behavior (no hand-rolled posture arrays,
no invented tool names). Check `get_scene` output first; if a capability truly
doesn't exist as a tool, say so — it's an app-feature gap, not a prompting
problem.
