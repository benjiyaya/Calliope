# Calliope 1.7.0 — agent engine rewrite and gap fixes

Release **v1.7.0**, 2026-11-14. The plugin harness is gone; the agent loop is a grant-scoped studio runtime, the capabilities it dropped are restored, and plain chat converses again.

## The agent loop has been rewritten

The old plugin harness (`harness/loop|orchestrator|policy|registry|tools`, `harness/plugins/*`) is gone, replaced by a grant-scoped studio runtime (`agent/runtime/` + `agent/capabilities/`):

- Your message is matched into **scoped studio calls** (story → script → assets → render → canvas). Each call runs ONE sub-agent that only sees its grant's tools, and the run is verified against the database — a write counts only when the grant's tables actually changed, never because the model said "Done."
- A failed call retries up to twice, carrying the previous transcript plus a recovery note that names the tools that ran.
- **Review cards are the only queue door.** The agent prepares prompts and references and stops; you press Generate. No prose approval games, no direct queueing.
- Steering works mid-run: send a message while the agent works and it lands at the next step boundary. Stop cancels the LLM turn **and** the GPU jobs (ComfyUI gets an interrupt).
- Every chat grant now shares one tool surface for scratch files (`list_files`/`read_file`/`write_file`/`delete_file` under `DATA_DIR/workspace/sessions/<id>`) and the opt-in shell (`run_command`, Settings → Agent) with path containment and an approval card.

## The gaps the rewrite dropped are fixed

The first cut of the rewrite silently lost capabilities the old harness had. All restored:

- **Live streaming** — assistant tokens and per-agent reasoning stream into the chat (`agent.token` / `agent.thinking`), and token usage lands on the final message when the endpoint reports it.
- **Multimodal attachments** — images (gated + downscaled, 4K included), videos (up to 8 evenly-spaced frames with a timestamp map), and documents (.txt/.md/.docx) reach the model, not just the event log.
- **@workflow and @skill mentions** are consumed as context again; the composer pickers work on canvas and Build Scene.
- **JSON-action bridge** — local models that can't emit function-call JSON still operate the studio (fenced `{"tool": …, "arguments": …}` replies execute through the same audited path, capped 8/turn).
- **Hardening + history budget** — the Settings → Agent hardening rules append to every sub-agent prompt; retry transcripts trim by a character budget so a long turn can't overflow a local model's window.
- **Build Scene poses** — the real 16-pose library is back (it was stubbed to two empty poses).
- **Question cards, memory, skills, contracts** — `question/asked`/`answered` events, the memory panel, runtime skill reading, and the contracts staleness gate all work against the new layout (877 → 901 tests green).

## Plain chat now converses

A message that names no studio verb used to get a canned "No studio change was requested." without the model ever being called. It now schedules one conversational turn — story-aware when the session is linked, canvas-aware in sandbox — and streams a normal reply. Attachments-only messages work the same way; the model sees the media and answers.

Status questions route as reads, not GPU writes: "is the render done?" or "check if the portrait is ready" no longer schedule an enqueue. "Wait for the video to finish" waits, then reports the outcome in the agent's own words instead of "The project has 1 job." A render/assets status turn is not declared "ready" while its jobs are still running — the agent is told to call `wait_for_jobs` or state the handoff.

## The workflow run-path is back on AI Canvas

Workflow cards are visible on the board again, with the full manual run loop:

1. **＋** (bottom-left) places a workflow node.
2. Drag from any media card's asset handle (characters, locations, items, and finished artifacts) onto a workflow card — the role chooser offers a **reference slot** (prompt/character/location/image/negative/audio/video, resolved onto the workflow's `(Input:role)` nodes at run time) or a **labeled story-bible note**.
3. **Run** merges the wired references over the node's saved input values and queues through the same pipeline as Playground. Unknown roles or empty sources fail with a named error before anything queues; the finished file materializes as an artifact card wired back to the node.

This is the manual path — it does not pass through the agent review gate. Stored wires render as dashed lines and Delete removes selected ones.

## Canvas and Projects quality-of-life

- **Session rename** — pencil icon or double-click in the session rail.
- **Revise** on a review card now puts the cursor in the card's editable prompt instead of typing a canned instruction into the composer.
- **Link to project** — a sandbox chat can be linked to an existing project from the title card, without asking the agent.
- **Open in AI Canvas** — every project card's ⋯ menu jumps straight to that project's board.
- **Project deep-links reuse sessions** — clicking "Draft Storyline" (or Regenerate Script) repeatedly reuses the same project chat instead of flooding the rail with duplicates.
- **Fewer refresh storms** — a multi-tool turn or a burst of job events coalesces into one board refresh per window instead of rebuilding the graph after every event.
- Tool events carry the agent that actually ran them (the board and logs no longer label every tool "script"); sub-agent reasoning prefixes stay correct in Build Scene.

## For contributors

- The engine contract lives in `AGENTS.md` → "Agent engine (studio runtime)": schedule → coordinator → subagent per grant, table-fingerprint verification, the review gate, steering watermarks, cancel semantics, and the conversational fallback (in `handle`, with attachments deliberately excluded from the schedule cue so path names can't trip the studio-verb regexes).
- `scripts/export_contracts.py` reads the new layout; `contracts.json` regenerated (149 names).
- New coverage for previously untested machinery: the SSE stream publisher, retry-history trimming, and the media projection ladder/cache.
