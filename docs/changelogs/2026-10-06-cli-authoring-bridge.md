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

- **`calliope-cli`** — 31 commands across 10 groups: `project`, `story`, `cast`,
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
- **opencode skill** at `skills_opencode/calliope/SKILL.md`.

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