---
name: scene-to-video
description: "Use when turning a Calliope scene into a video job — chaining from a previous clip, ordering reference images, or picking duration settings."
version: 1.0.0
license: MIT
metadata:
  author: Calliope
  tags: [video, scenes, comfyui, minimax-h3, references]
---

# Scene to Video

## Overview

Recipe for driving a scene through Calliope's video path: draft prompt → resolve
references → enqueue → track the artifact. The machine contract (role tags,
profiles) is enforced by Calliope; this skill is the judgment layer on top.

## When to Use

- The user asks to generate video for one or more scenes
- A scene should continue from the previous clip (`chain_from_prev`)
- Reference ordering matters (which character/image is `<Subject 1>`)

## The Flow

1. **Read the scene first** — `get_workspace` or `list_scenes` for the scene's
   draft text, characters in scene order, and location. Never enqueue from a
   summary alone.
2. **Prompt style follows the workflow's `prompt_profile`** — `prose` (flat
   paragraph) or `minimax_h3_ref` (six-section, rewritten by an LLM at enqueue
   time). Don't hand-format H3 sections unless the user asked for a raw prompt;
   the profile owns the format.
3. **References fill slots in node-id order**: characters in scene order, then
   the location, capped at the workflow's `(Input:image)` slot count. `<Subject 1>`
   = lowest node id. If a specific character must be Subject 1, say so and let
   the user re-wire the data edges on the canvas.
4. **Continue scenes** need the workflow to have an `(Input:video)` node. Auto
   mode takes the nearest earlier clip; if it doesn't exist, surface the error —
   don't retry blind.
5. **Confirm before enqueue** — use `ask_user` with `scope:"render"` when the
   user hasn't explicitly asked to generate. Then `enqueue_video_jobs` with the
   scene_id, and track via the returned job ids.

## Failure Handling

- `job.failed` with a Comfy node name in the error: report the node and its
  likely input gap (missing ref file, bad dimension pair).
- Poll timeout: the queue may still be busy — check `get_workspace` job status
  before re-enqueueing; double enqueues waste GPU-hours.
