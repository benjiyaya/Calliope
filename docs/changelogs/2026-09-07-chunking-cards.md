# Calliope 1.4.1 — chunked generation + working question cards

Two fixes to the agent pipeline, both aimed at long-running generations
stalling without feedback.

## Fixed

- **Draft Storyline / Regenerate Script no longer hang on long targets.**
  Both asked the LLM for the whole board in one response — a 50-beat
  storyline or a 20-scene script is a multi-thousand-token JSON answer that
  small/local models take minutes to stream, often under-deliver, and then a
  single short chunk retried the *entire* generation. Story beats are now
  drafted in chunks of 12 (a brief call with title/logline/cast, then
  continuations that see the established cast and recent beats); script
  scenes in chunks of 4, each fed the full context plus the last two written
  scenes for continuity. Both renumber their boards gaplessly, retry only a
  short chunk, and publish progress per chunk ("Writing beats 25–36
  (chunk 3/5)…"). Small projects keep the exact single-call behavior.
- **The agent's question cards actually appear.** When the agent asked you
  to confirm something (regenerate a script, replace content, render), the
  chat froze at "working…" with no question on screen — you had to press
  Stop or refresh to see anything. The turn now records the question before
  ending, and the card renders it with clickable options. Clicking an option
  or typing "yes" both count as the approval; "No, …" refuses.

## Notes

- The agent loop is unchanged otherwise — chunked calls are internal to the
  story/script generators, so the AI Canvas agent tools (`generate_story`,
  `generate_script`) benefit automatically.
- If a long generation still feels slow, the progress events now show which
  chunk the model is on — a stuck chunk means the LLM endpoint is the
  bottleneck, not Calliope.

## Upgrade

```bash
git pull
```

No new dependencies, no schema changes (existing boards are untouched —
regeneration just gets faster). Restart the backend and the frontend dev
server.
