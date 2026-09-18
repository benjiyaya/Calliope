# Calliope 1.5.4 — steering input, Asset Library, canvas delete, agent hardening

A feature release in three waves: the agent loops got the safety nets and
capabilities from the loop evaluation, AI Canvas got honest deletion, and
unlinked Playground media got a home of its own.

## What changed

### Steering input (AI Canvas + Build Scene)

- Messages sent **while the agent is running** now steer the turn instead of
  failing with a 409. `POST /sessions/{id}/messages` detects a running turn and
  appends a `steering/message` event + a mirrored `status='steering'` chat row.
- Both loops drain steering **at step boundaries only** — a user message
  injected between an assistant `tool_calls` message and its results is an
  invalid OpenAI sequence, so history derivation buffers mid-exchange steering
  until the exchange closes. Injected turns are labelled
  `[STEERING — user message sent while you work]`.
- Steering course-corrects; it can never **grant** permission. Render /
  destructive approval still derives from the latest `user/message` only.
- Composer: editable + Send while running (button reads "Steer"), Stop stays
  put; steering bubbles render with a distinct marker in the chat.
- Blind sessions skip the planner entirely — the Build Scene agent can only
  misroute there, so it runs the single loop.

### Agent loop hardening (7 fixes from the loop evaluation)

- **Swarm safety nets.** Sub-agents now run the same guards as the main loop —
  shared `RepeatGuard`, fail-streak `[SYSTEM DIRECTIVE]`, and a final-step
  nudge. A sub-agent `ask_user` **pauses the whole swarm**: remaining tasks are
  marked `skipped` (new task status, surfaced in the plan panel) and the turn
  ends with the question card; replying continues naturally.
- **Clip tools in swarm roles.** `script` gains
  `list_clips`/`break_into_shots`/`add_clip`/`update_clip`/`delete_clip`,
  `video` gains `list_clips`; a category→role drift test prevents a repeat.
- **Deleted the unfulfillable ComfyUI exception** from the Build Scene prompt —
  the tool scope guard has no such path, and the prompt was teaching the agent
  to promise one.
- **`request_capture` no longer lies.** The 10s timeout returns `ok: False`
  with `capture_pending: True` and explicit "do NOT reference a file path"
  guidance, feeding the fail-streak guard instead of fabricating successes.
- **Planner fast-path.** Trivial single-verb goals skip the planner LLM call
  and go straight to the single loop; blind sessions no longer pay a wasted
  workspace read before early-outing.
- **Per-tool wall-clock budget.** New `agent_tool_timeout_sec` setting
  (default 600, `0` = off) wraps `registry.execute` in `asyncio.wait_for`;
  `wait_for_jobs` is exempt (`long_running=True`, it owns its timeout contract)
  and now publishes `agent.thinking` progress while polling.
- **Token-bounded history.** `derive_llm_history` gains a character budget
  (`agent_history_char_budget`, default ~400k chars) that drops the oldest
  whole turns — turn-count alone let long sessions overflow the context window.

### AI Canvas deletion that deletes

- Backspace/Delete on artifact cards now **persists**: the frontend
  `onbeforedelete`/`ondelete` handlers call the backend, which hard-deletes the
  artifact node row, tombstones entity nodes, and publishes `canvas.updated`
  so the refetch cannot resurrect the card.
- **File cleanup is rule-based, not blind.** The backing file is deleted only
  when it lives in playground-managed folders (the `uploads/` library or the
  scratch project's output folders) **and** nothing references it — entity
  images, scene/clip videos, or another live canvas card. Project-owned files
  are replace-only and always survive; the toast says which happened
  (`file_deleted` / `reason`).
- Deleting a card was the only way to "remove" a stray Playground generation
  before; the reappearing-card bug made that impossible. Fixed.

### Asset Library (`/library`)

- New page listing **unlinked** Playground media — the `uploads/` library plus
  the scratch project's image/video outputs. Files referenced by project data
  are excluded: they live in Project Assets and are replace-only.
- Grid with **All / Image / Video filters** (live counts), multi-select,
  **bulk delete** (max 200 per request; referenced/project-owned/foreign paths
  are kept and reported per-path), and **Add to Project** via the existing
  attachment modal.
- **Lazy loading at 50 per page** — an `IntersectionObserver` fetches the next
  page as you scroll, with a manual "Load more" fallback.
- Backend: `GET/DELETE /api/library/media`; new nav link in the header;
  localized in all seven languages.

### Build Scene: Brief/Cut gates + MP4 export

- The shot-composer-blockout agent now works behind two human gates: **Brief**
  before scene mutations and **Cut** before export (`get_build_scene_gates` /
  `record_build_scene_gate` tools; soft warning when mutating without a brief;
  the UI records gates with `source: 'ui'`; `clear_scene` preserves them).
- New guard code `guard_no_export_video_tool` — the Build Scene agent has no
  export tool and is stopped honestly at the gate instead of guessing.
- Export video normalizes to **H.264 MP4 at ingest** (`_transcode_video_to_mp4`,
  `libx264 + yuv420p + faststart`, 503 when ffmpeg is missing) so the project
  clip picker always receives a real MP4 regardless of the recording browser.

## Upgrade

No DB migration and no new frontend dependencies; the backend gains
`pillow`. `git pull`, then `uv sync` in `calliope-backend/` (or let the start
script do it) and reload the frontend. New settings
(`agent_tool_timeout_sec`, `agent_history_char_budget`) default sensibly —
nothing to configure.
