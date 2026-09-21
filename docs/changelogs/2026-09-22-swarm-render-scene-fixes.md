# Calliope 1.5.5 — swarm fixes: renders enqueue, scenes get written, the board fills

A regression-fix release for 1.5.4's agent hardening: live event logs showed a
planner-scheduled render task whose tools were hidden, a story sub-agent denied
the scene tools the planner told it to use, deep-linked chats orphaning as
duplicate empty sessions, and new project assets not appearing on the canvas
until re-open. All four paths are fixed and regression-tested.

## What changed

### The "generation tool is not exposed" deadlock (video sub-agent)

A swarm render task could dead-end in three layers at once: the planner said
"enqueue_video_jobs for those 8 scenes", the HITL visibility rule hid every
render tool (no render verb in the user's words), and the sub-agent — unable to
call or even see the tool — ended with *"enqueue could not be performed — the
generation tool is not exposed in this session's toolset"*.

- **`clip` / `shot` / `storyboard` are render-intent cues now.** "Break the
  beats into the 8-scene script + shot clips" is a generation order; the
  clause-scoped negation rules are unchanged.
- **Sub-agent payloads keep approval-gated tools visible.** The scoped payload
  filters `requires_project` / `blind_only` / scene-scope only; permission is
  enforced at execute time by the `_render_approval_guard`, whose denial names
  the recovery path. A visible, guard-denied tool is recoverable — a hidden one
  is a wall.
- **The denial teaches `ask_user`.** A gated sub-agent now asks the user to
  confirm generation; the affirmative answer unlocks the retry. HITL itself is
  untouched — nothing renders without an explicit user ask.

### Story sub-agent can structure beats into scenes

The planner scheduled "create exactly 8 scenes via add_scene" as a **story**
task, but `add_scene` lived only in the script role — the story agent reported
`Tool not available to this role: add_scene`, scenes were never created, and
the script pass then found "no scenes in the project" (the "nothing inserts
into story and script" report).

- The story role gains `list_scenes` / `add_scene` / `update_scene` /
  `delete_scene`.
- The planner system prompt gains an explicit role-boundary rule: story owns
  beats, script owns scenes — never schedule `add_scene` under story.

### No more orphaned duplicate deep-link sessions

The Project stages' "Draft Storyline" / "Regenerate Script" buttons create a
session and hand off with `?session=<id>` — but the canvas page validated that
param against a possibly-stale cached session list, missed the milliseconds-old
session, nulled the active chat, and the next Send created a **second**
session. Result: a pile of empty "Draft Storyline" sessions in the rail plus
one that actually worked. The deep-link param is now trusted outright; a truly
gone session just 404s in the query, which is recoverable.

### Canvas auto-seeds new entities on refetch

Entity cards (characters, locations, items, scenes) only seeded when a canvas
was *ensured*, so assets the agent created mid-session stayed invisible until
you re-opened the board. `GET /api/canvas/{id}` now auto-seeds missing entity
cards — idempotent, tombstone-respecting (deleted cards never resurrect), and
it publishes `canvas.updated` only when something was actually added.

## Testing

- `tests/test_render_intent.py` — clip/shot cues, including the exact session
  message that failed in the wild.
- `tests/test_tag_render_permission.py` — story-role scene tools; sub-agent
  payload keeps `enqueue_video_jobs` visible without intent; the execute-time
  guard still blocks and names `ask_user`.

Full stack: 177 backend tests across the nine affected suites plus
`svelte-check` (0 errors).
