# Calliope 1.5.0 — Build Scene blockout + scenes → clips pipeline

Two large additions: a **3D blockout shot composer** for previsualizing
shots before AI generation, and a **pipeline redesign** so script content
actually performs on screen (one script scene → many shot clips).

## Build Scene: 3D blockout shot composer

A new top-nav module. Instead of describing camera, framing, and pose in a
text prompt and hoping, you block the shot out in 3D first — pose figures,
place blocks, keyframe motion, frame the camera — then feed the rendered
result into your project as a reference image/video.

<img width="1781" height="1248" alt="Screenshot 2026-09-11 060020" src="https://github.com/user-attachments/assets/f8b39354-de28-4e6b-a055-b91c6ee33a4a" />

- **New "Build Scene" tab** with the same three-panel shell as AI Canvas:
  scene tree + inspector (left), 3D viewport (center), agent chat (right).
- **Agent-driven blockout:** describe what you want in chat — the agent
  builds it through built-in tools (`add_object`, `set_transform`,
  `set_joint`, `set_shot`, `add_keyframe`, …). No MCP server; tools are
  native, same as the rest of the harness.
- **Multiple scenes** via the left session rail: each `Scene · N` entry is
  its own blind session owning its own composition. Nothing is overwritten
  when you start a new scene. These rail entries never appear in the AI
  Canvas session list (sessions carry an explicit origin).
- **Tool hardening:** the Build Scene agent's toolset is exactly the shot
  tools plus ask_user/memory/skills — it cannot call `run_workflow`,
  enqueue jobs, or touch projects, enforced mechanically (payload filter +
  execute-time denial), not by prompt.
- **Primitives + mannequins:** boxes/cylinders/etc. plus articulated
  mannequin figures (male/female/child) with a 26-joint pose tool — drag
  joint handles directly in the viewport.
- **Viewport toolbar:** Select / Move / Rotate / Scale / Pose tools,
  Undo/Redo (50 steps), Grid toggle, Composition (rule-of-thirds) overlay,
  and Capture.
- **Keyframe motion:** stage a transform/pose, add a keyframe at a time,
  stage the next, add another — the viewport samples the track per frame
  (smooth position/rotation/posture interpolation). Timeline strip shows
  camera and object keys on one playhead; drag to retime, click to inspect,
  double-click to delete. Playback capped at 60s.
- **Camera timeline + video export:** key the live camera from any orbit
  position, play the track back, and Export video renders one deterministic
  pass (MediaRecorder, MP4→WebM fallback, ≤ 60s) saved as a video capture.
- **Captures become references:** PNG and video captures live under the
  asset store and appear in every project reference picker under a
  **From Build Scene** group (`· blockout` label) — pick one into an
  `(Input:image)` slot like any other reference. This is the ControlNet
  conditioning path: blockout first, then generate on top of it.
- **Vision input:** attach images *and videos* to the chat (Build Scene and
  AI Canvas). A 10s fight clip, for example, is projected into the LLM as
  evenly-spaced frames with timestamps — "recreate this motion as 3D
  blockout" maps frames to keyframes. Images render as real image parts,
  not text guesses.
- **Collapsible chat** so the canvas gets full width; preference persists.
- **AI Canvas: document attachments.** Attach `.txt` / `.md` / `.docx`
  (≤ 5 MB) — your own screenplay, for example — and the agent drafts the
  storyline (`create_project → generate_story → generate_script`) directly
  from it, through the same attachment pipeline as images.

## Scenes are script units, clips are render units

Real video production never films a scene as a single take — a 90-second
dialogue scene becomes a wide establishing shot, medium shots, close-ups,
inserts. The old model fused those two units together (one scene = one video
clip), so long scenes could not actually be performed: 20 lines of dialogue
had no shots to live in, and video models only render ~5–10 seconds per
clip anyway.

```
Script → scenes (screenplay units) → 1 : N → clips (shot units) → jobs
```

- **Scenes** now hold the screenplay: heading, full action prose, verbatim
  `SPEAKER: line` dialogue. No more compression into thin summaries.
- **Clips** are what render: each has a description of what the camera
  sees, a shot size (wide/medium/closeUp/insert/overShoulder), its share of
  the scene's dialogue (`dialog_lines_covered`), a duration ≤ 8s (default
  cap), and its own workflow/render/settings/continue-chain state.
- **Invariant: every scene has ≥ 1 clip.** A scene you never expanded
  auto-carries a default clip inheriting all its production fields — an
  un-expanded project renders exactly like the old 1:1 model. The last
  remaining clip of a scene is protected (delete the scene instead).
- `#3` scene labels became `#3.1, #3.2, …` clip labels everywhere: job
  names, agent tools, the prompt review modal.

### Script fidelity

Script generation no longer forces "ONE SHOT PER SCENE" and no longer
summarizes. Scenes preserve the full action prose and verbatim dialogue,
and scene durations are **derived from content** (dialogue at ~150 wpm +
action beats), scaled so the board's total lands on the user's target
runtime. A "30 minutes" ask now produces scenes worth ~30 minutes of
material.

### Break into shots (coverage expansion)

New LLM pass, per scene: it reads the scene's full content plus the cast
and neighbors, then allocates **every dialogue line and every significant
action beat** across clips of ≤ 8s, with durations summing ≈ the scene's
budget. Each expanded clip carries only the dialogue lines it performs —
which is exactly what the per-clip video prompt becomes (heading +
`[shot_size]` description + covered lines). Replacement is transactional
per scene, the scene's continue-chain migrates onto clip #1, and progress
publishes ("Breaking scene 6 into shots (2/14)…").

Found in the UI: **Break into shots** per scene + **Break All Into Shots**
in the Script stage. The AI Canvas agent has it too (`break_into_shots` —
destructive, so bulk scope still needs an explicit ask).

### Pipeline re-scoped to clips

- Video jobs are clip jobs (`jobs.clip_id`); enqueue/supersede/clear-render
  operate per clip, iterating **global playback order** (scene order, then
  clip order within the scene).
- Continue-from-previous walks the clip order (a continue clip extends the
  nearest earlier *clip*).
- Prompt review/preview accepts `clip_id`; confirmed prompt drafts persist
  per clip with a clip-aware staleness hash.
- Export stitches clips in playback order; worker write-back lands on
  `clips.clip_path` (mirrored to the legacy scene field for old readers).

### UI

- Script stage: per-scene clip sub-lists (shot size, duration, covered
  dialogue, per-clip status) with add/edit/delete/reorder and the
  Break-into-shots buttons.
- Video stage: generate-all enqueues **per clip**, the filmstrip and
  monitoring show `#N.M` clip slates, totals/counts come from clips, and
  the clip-source picker (for video-extend continues) lists individual
  clips.
- Canvas scene cards play the scene's first ready clip.

## Upgrade

```bash
git pull
npm install   # in calliope-web/ — new three + mannequin-js deps
```

Restart the backend and the frontend dev server.

- **Existing projects are safe.** A one-time migration backfills one
  default clip per existing scene (copying duration/workflow/render/
  settings/chain) and points existing video jobs at their scene's default
  clip, so existing projects keep rendering unchanged. Un-expanded projects
  behave exactly like the old 1:1 model until you Break them into shots.
- The `shot-composer-blockout` skill seeds automatically on first Build
  Scene use.
