# Calliope 1.5.8 — story continuity plans

MiniMax H3 clip prompts now come from one film plan per project. The Video tab
and a project-linked AI Canvas chat share that plan, so a clip compiled in
either place keeps the same style, speakers, camera, and lighting as its
neighbors.

This is the agentic reading of a timed shot plan plus a nine-check critic. The
H3 six-section compiler is unchanged. No new model is trained.

## One ledger for the project

`projects.continuity_json` holds a single document:

- **Overview** — style, pace, soundscape, camera grammar, lighting.
- **Requirements** — nine short locked lines: style, subjects, actions,
  dialogue, sound, camera, lighting, spatial relations, scene.
- **Shots** — one entry per clip in playback order: clip id, time window,
  action, speaker bindings, camera, lighting, spatial note, and what changes
  after the shot.
- **based_on** — a hash of the script text, clip ids and order, and reference
  paths. A story edit or a new reference file marks the plan stale.

Refresh is lazy and shared. The Video tab preview and video enqueue call the
same helper: a matching hash is a no-op; a mismatch rewrites the whole plan in
one model call from the screenplay, the shot list, character and location
text, and the reference files already on the clip. Clips that already have a
rendered file are listed as realized footage. If that model call fails, the
board itself becomes the plan, and preview still returns.

The script role can write the plan early with `refresh_continuity_plan`, after
the script or Break into shots and before render.

## The compiler phrases the locked slice

The H3 rewrite still produces the six sections. Its instructions for this clip
now include that clip's ledger slice plus the previous and next shot. Those
lines are binding: the compiler may phrase them into the six sections, and it
keeps the subject, the speaker of each line, the lighting, and the screen
direction. Reference images and the reference video stay the identity and
motion source. Story location text fills an image slot only when that slot was
left empty.

A saved prompt draft includes the plan hash, so a new plan marks the draft
stale. Regenerate still forces a fresh compile.

## Continuity check on the Video tab

After compile, preview runs a second model call. It scores the candidate on
the nine checks and the neighbor shots, and it returns only failures
(omission, wrong binding, contradiction, order). The prompt modal shows those
notes above the editor under **Continuity check** (localized in all seven UI
languages). Generate stays available. The modal is still the human gate.

Generate All compiles every clip from the ledger, so a batch cannot skip the
plan. It does not run the critic and does not wait on it.

A dead critic never fails the request. A compiled candidate falls back to the
deterministic H3 template with `critic.ok = false` and a short reason. A draft
the user already saved is kept.

## Where it runs

- **Project → Video.** Preview, Generate, and Generate All on a MiniMax H3
  workflow.
- **AI Canvas, project-linked.** The chat can call `refresh_continuity_plan`.
  When that agent enqueues video, it compiles from the same ledger the Video
  tab reads. The canvas chat does not show the critic notes.

Sandbox canvases have no project ledger. Prose workflows keep the prose
prompt. Build Scene is unchanged.

## Testing

- `tests/test_continuity.py` — stale plan refreshes, a matching hash is a
  no-op, neighbor-shot lock in the H3 message, a filled reference slot stays
  the user's file, preview returns critic notes, a dead judge falls back to
  the H3 template, a saved draft is kept, and `refresh_continuity_plan` is on
  the script role.
- Existing H3 preview and enqueue tests stay offline: they stub the plan and
  the critic so a live model is never required.
- `contracts.json` includes the new tool name.
