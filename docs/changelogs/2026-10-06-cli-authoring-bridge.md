# Calliope 1.6 — CLI authoring bridge (`calliope-cli`)

A second way to write a project's *content*. Until now the web UI was the only
door in, which meant a 34k-character novel could only become beats by pasting
fragments into a textarea and waiting for the built-in agent to summarise them
in one pass. `calliope-cli` is that door for tool callers — opencode in
particular, and anything else that speaks JSON.

It writes finished text: story beats, cast, screenplay scenes, shot clips,
continuity requirements, and the source novel itself. It never calls a model,
never touches images or video, and never deletes anything.

## What changed

- **`calliope-cli`** — 32 commands across 10 groups: `project`, `story`, `cast`,
  `script`, `clips`, `context`, `shots`, `plan`, `log`, `schema`. Run it as
  `calliope-cli.bat` at the repo root.
- **`plan next`** decides what to write, and says why. Ordered
  beats → cast → script → clips → continuity, with the counts behind the
  decision and the scene ids still to shoot.
- **`schema show`** prints the JSON Schema for any payload, generated from the
  same Pydantic models the validator uses, so the documented contract cannot
  drift from the enforced one. `schema validate` checks a payload without
  writing.
- **`project source`** reads a long novel by character window or by grep, so a
  caller pulls the 4k characters it needs instead of the whole text.
- **`project set --idea-file`** ingests the novel verbatim. `--idea` cannot
  carry a newline through `cmd.exe`, so multi-line text must arrive as a file or
  on stdin — otherwise the command line truncates silently at exit 0.
- **`[CARRY]` blocks** in `clips.description` carry per-shot continuity
  constraints, which is where they belong: the ledger's per-shot half is
  derived and would be discarded on the next recalculation.
- **Append-only audit** with per-row before-images, recorded in the same
  transaction as the change. `log list` / `log show` answer "what changed".
- **`story order`** reports a beat sequence that is not a dense `1..N`, naming
  both the holes and the positions two rows are fighting over. Scenes and clips
  are renumbered on every write; beats deliberately are not, because the web UI
  is allowed to leave gaps. That tolerance would otherwise hide a mistake
  forever — `story list` cannot show a hole, since "beat 3 is absent" looks the
  same as "the story ends here".
- **opencode skill** at `skills_opencode/calliope/SKILL.md`.

## Storing a novel in the same field as a pitch

`projects.idea` holds both the logline the built-in generators were written
against and, now, the novel `calliope-cli` writes into. One column, two
meanings, and three consequences:

- **The built-in generators refuse a novel.** Past 2,000 characters
  `generate-story` and `generate-script` return 422 and say to use the CLI —
  checked *before* the prompt is built, so a refusal costs nothing. They would
  otherwise spend a real call on 34k characters the model cannot use, and
  return beats that quietly ignore the novel. Truncating would hide that;
  refusing is the honest version. The threshold is the repository's own
  (`workspace.py`'s existing `project_idea_large` warning), not a new number.
- **The continuity planner skips the model instead of refusing.** It runs
  inside the render loop, so raising there would break rendering for exactly
  the projects this feature exists to serve. It already falls back to a
  board-derived plan when the model is unavailable; a long `idea` now selects
  that fallback on purpose. That is also the better answer — the plan describes
  the board, which is what the ledger is supposed to describe.
- **`expand-clips` is deliberately not guarded.** It never reads `idea`, so
  guarding it would block a working operation in exchange for nothing. "Break
  into shots" stays available on a CLI-authored board; it is how a user refines
  shots without giving up the text they wrote.

A **project list no longer ships the novels**. Both `GET /api/projects` and
`calliope-cli project list` return the first 200 characters as `idea_preview`
and null out `idea`; the single-project responses still carry the full text,
because that is the one an editor loads and edits. The list is the response
that grows with (projects × novel length) and reloads on every visit, so four
long-form projects was already 400KB+ of JSON for a description the card clamps
to two lines. Search now covers the preview — unchanged for every logline
project, whose preview *is* the whole text.

The web UI **hides the two generate buttons** ("Draft storyline", "Regenerate
script") on an `ingest_mode='external'` project, replacing them with a one-line
explanation in all seven locales rather than leaving a dead button that cannot
say why. Length and `ingest_mode` are complementary signals: the web form has
no mode control, so a novel pasted there is still `builtin` and still gets the
422.

## Drift: read → hash → write

The user edits in the web UI while a tool is working. A write built on a read
from ten minutes ago silently overwrites their edit, so every write takes
`--expect-hash` and compares it **inside** the write transaction:

```bash
calliope-cli project hash --project 7 --json
calliope-cli story list  --project 7 --json
calliope-cli story append --project 7 --file beats.json --expect-hash 4f2a9c1e8b7d6053
```

Row-level content hashes are the only mechanism that works here: the tables that
hold the content have no `updated_at` column at all, so there is no timestamp to
compare. Refusal is **exit 3**, distinct from exit 2 (your payload is wrong) —
the fix is to re-read and re-apply, not to retry the same payload.

## Boundaries — not configurable

- **The CLI cannot delete a project.** No `delete` verb, no flag that enables
  one. The web UI deletes, and that is also what removes the project's audit
  rows, snapshots, and on-disk mirrors.
- **The CLI never calls a model.** Nothing under `cli/` can reach
  `llm.generate` or `ensure_continuity_plan`.
- **No images, no video.** Those columns are read-only here.
- **No raw SQL, no shell, no filesystem writes** outside the audit and snapshot
  directories.
- **Unknown fields are hard errors.** A typo'd key that was silently dropped
  would look like a successful write that did nothing.

## Upgrade

Idempotent schema migration: two columns on `projects`
(`project_revision`, `ingest_mode`) and a new `cli_audit` table, all added with
`ADD COLUMN`-style guards. No existing column changes type or default, so old
databases migrate without a rebuild. `git pull`; the frontend is unchanged.