# Calliope 1.4.0 — AI Canvas

The **AI Canvas** (`/canvas/[id]`) merges the old Agents chat and Playground
into one node-graph surface: your project's story bible as cards on a canvas,
with the agent chat hosted in the right panel and the session list as a
collapsible left rail. The separate `/agents` and `/playground` pages now
redirect in.

## New

- **Merged Canvas surface.** A `@xyflow/svelte` graph of characters, locations,
  items, and scenes. One durable canvas per project (the story bible, shared
  across its sessions) plus one sandbox canvas per unlinked chat, so a
  free-flow session can generate images and videos and see the results without
  committing to a film.
- **Entity auto-seed.** Opening a project canvas adds a card for every
  character, location, item, and scene — idempotent, so reopening only adds
  new entities. Deleting a card tombstones it and it never resurrects. Card
  media resolves live (character sheet, location/item reference, scene clip).
- **Edges + run-path.** Drag a data edge from an entity/artifact card into a
  workflow node to wire a `(Input:role)` slot (role-resolved, cycle-checked);
  Shift-drag makes a labeled story-bible link edge instead. **Run** on a
  workflow node enqueues through the existing pipeline; the finished output
  materializes as an artifact card.
- **Agent canvas tools.** The chat agent can read and edit the board:
  `summarize_canvas`, `create_canvas_node`, `canvas_connect`, `canvas_link`,
  `update_canvas_node`, `run_canvas_node` (HITL-gated), `post_artifact_to_canvas`,
  `delete_canvas_node`.
- **Skills.** Reusable expertise the agent loads on demand — standard
  `<data_dir>/skills/<name>/SKILL.md` folders with YAML frontmatter. Three
  built-ins ship seeded (H3 video prompts, scene-to-video, character
  consistency). Manage them in **Settings → Skills**; type `/` in the composer
  to tag one, and the agent reads it before applying.
- **Memory.** Durable user preferences and project conventions the agent
  carries across chats. It saves when you state one ("always give me 16:9",
  "never show the villain's face"); saved memories are injected into every
  turn. Manage them in **Settings → Agent → Memory**.
- **Question cards (HITL).** When the agent needs an explicit choice, it asks
  with a clickable card (`ask_user`) and ends its turn until you answer — a
  click records a structured approval instead of the agent guessing from prose.
- **Tidy layout.** One click re-arranges every card into clean columns and
  grids.

## Fixed

- **Stop actually stops.** The Stop button now cancels the agent turn *and*
  the GPU jobs it enqueued — pending/running jobs for the session are
  cancelled and the running ComfyUI prompt is interrupted, so generation halts
  immediately instead of only killing the LLM call.
- **Canvas drag positions persist.** Group/box drags now save every moved
  card (previously only one was written), and an SSE-driven refetch can no
  longer snap a card back mid-drag.
- **Card media renders correctly.** Artifact images fill their frame instead
  of showing a cropped corner; clicking any card — scene clip or job output —
  opens the same modal player.
- **Agent can generate in sandbox.** Canvas artifact cards no longer
  double-post (unique per-job guard), and a sandbox session can post to its
  own board without being told to attach to a project.
- **Settings → Skills / Memory pages** navigate correctly (tab switching
  previously left the URL changed but the panel stale).
- **Browser tab title** is now "Calliope Lab".

## Notes

- The agent's render permission comes from explicit user acts only — an
  `@workflow` tag, a question-card approval, or your own words asking to
  generate. Mass-generation is guarded mechanically by the enqueue tools
  (explicit ids, or an "all/remaining" request), not by guessing at prose.
- Canvas titles derive from what they show: project canvases take the project
  name, sandbox canvases the session title.

## Upgrade

```bash
git pull
cd calliope-web && npm install
```

New frontend dep: `@xyflow/svelte`. Backend DB auto-migrates on start (adds
`canvas`, `canvas_node`, `canvas_edge`, `agent_memory` tables). Restart the
backend (FastAPI) and the frontend dev server (Vite) to pick everything up.
