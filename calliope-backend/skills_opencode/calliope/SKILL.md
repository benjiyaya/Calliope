---
name: calliope
description: >
  Write finished Calliope story content — beats, cast, screenplay scenes, shot
  clips, continuity requirements — into an existing Calliope project through the
  `calliope-cli` bridge. Use when a Calliope project needs story content authored
  or revised, especially a novel whose source text already sits in the project's
  Story idea field, or anything too long for Calliope's built-in agent to write in
  one pass. Triggers: "写故事线", "读小说原文", "生成剧本", "分镜", "story beats",
  "screenplay", "break into clips", "continuity requirements", "写进 Calliope",
  or any mention of authoring Calliope content from outside the web UI.
  Not for generating images or videos — those stay in the web UI.
---

# Calliope CLI 创作桥

The CLI is the only way content enters a Calliope project from outside the web
UI. It writes finished text: beats, cast, scenes, clips, continuity requirements,
and the source novel itself. It never calls a model, never touches images or
video, and never deletes anything.

## The one rule

**Read → hash → write → verify.** Every time, for every group.

The user edits in the web UI while you work. A write built on a read from ten
minutes ago silently overwrites their edit. So:

```bash
calliope-cli project hash --project 7 --json      # fingerprints, right now
calliope-cli story list --project 7 --json        # the content you are acting on
calliope-cli story append --project 7 --file b.json \
    --expect-hash <the story_beats value from step 1>
```

`--expect-hash` is what makes this safe. It is compared **inside** the write
transaction, so nothing can slip in between the check and the write. Skip it only
for `project create`.

## Find the project

```bash
calliope-cli project list --json          # id, title, status, counts
calliope-cli project show --project 7 --json
```

`--json` and `--dry-run` go at the end of the line, as above. They are also
accepted in front of the command path — `calliope-cli --json project list` gives
identical output. Prefer the trailing form; it reads as one command.

`project show` returns counts and the plan state. The source text is *not*
included — add `--with-idea` when you need it, because a novel is 34k+ chars and
`plan next` summaries must stay readable.

## Read the source text

The long-form novel lives in `projects.idea`. Do not load all of it:

```bash
calliope-cli project source --project 7 --from-chars 0 --to-chars 8000 --json
calliope-cli project source --project 7 --grep "老陈" --context-lines 4 --json
```

Both return an **object**, not a bare string: `{text, chars, lines, is_long,
from_chars, to_chars, truncated}`, or `{chars, lines, is_long, matches}` for
`--grep`. `chars` is the size of the whole text even when you read a window, and
`is_long` is true past 2000 characters. Each `--grep` hit carries `line`,
`context_before`, `context_after`, and the list is capped at 200 — re-grep with a
narrower needle rather than trying to page it.

Slice by character offset or grep, repeatedly, until you have what you need. Then
close the loop with `context set` requirements.

Writing a novel in? Use a file, always:

```bash
calliope-cli project set --project 7 --idea-file novel.txt
```

**Never pass a multi-line novel to `--idea`.** `calliope-cli.bat` goes through
cmd.exe, and `%*` re-expansion treats a raw newline as the end of the command
line — everything after it is dropped, exit code still 0. You would write a
truncated novel and lose the rest of your flags with no error. `--idea` is only
safe for a single line. (`--idea-file -` reads stdin, which is also fine.)
Giving both `--idea` and `--idea-file` is exit 2: they name the same column.

`ingest_mode` is set once, at `project create --ingest-mode external`. It is not
editable afterwards — check `project show` rather than trying to change it.

## What to write next

Do not decide this yourself. The CLI knows the ordering and why:

```bash
calliope-cli plan next --project 7 --json
```

```json
{
  "project_id": 7,
  "step": "cast.upsert",
  "why": "beats exist but no cast; scenes reference characters by name",
  "counts": {"story_beats": 24, "characters": 0, "scenes": 0, "clips": 0},
  "plan_state": "missing",
  "basis_hash": "…",
  "hint": "calliope-cli schema show cast upsert"
}
```

`step` is one of:

| `step` | do this |
|---|---|
| `story.append` | write the beat list from the source text |
| `cast.upsert` | write characters, locations, items |
| `script.append` | write scenes |
| `clips.append` | expand scenes into shots (see `targets` for scene ids) |
| `context.set` | write `overview` + `requirements` (the ledger is not yet authored) |
| `render` | stop — everything is written; the user renders in the web UI |

`clips.append` and `context.set` also return `targets` (the scene ids still to
shoot). Any step may return a `note` — anything true but **not** a reason to stop
what you were doing. `plan next` never gates on a note; it also uses one to report
a gapped or duplicated beat sequence (`story order` has the detail). When several
notes apply they are joined with `" | "`.

The order is beats → cast → script → clips → continuity, and it is not negotiable:
a scene names characters by name, and a continuity ledger describes shots that
must already exist.

## Payload shapes

Never guess a field name. `schema show` is generated from the same Pydantic
models the validator uses, so it cannot drift:

```bash
calliope-cli schema show                       # every group/verb
calliope-cli schema show story                 # the verbs in one group
calliope-cli schema show story append          # {group, verb, accepts, payload, schema}
calliope-cli schema validate --group story --verb append --file b.json
```

The inner `schema` is the JSON Schema; `payload` says whether the file is an
`array` or an `object`. Where a field takes a closed set of keys rather than free
strings — `context set`'s ledger buckets — `schema` carries it as
`propertyNames.enum` **and** `x-allowed-keys`, matching what the validator
enforces. An unknown group or verb is exit 4, not an empty object.

`schema validate` checks a payload without writing. Use it before any batch you
are unsure about — it costs one process and catches every field mistake at once.

Compact reference (`*` = required):

```
story append / replace-range  -> [array]   title*, description, order_index
story update                   -> flags    --beat-id --title --description --order-index
cast upsert                    -> [array]  name*, kind(character|location|item), role, age,
                                          appearance, personality, description, reference_image_path
cast update                    -> flags    --entity-id --kind --role --age --appearance
                                          --personality --description
                                          --reference-image-path
                                          --portrait-path --sheet-path --consistency-prompt
                                          (last three: clear a stale ref with "" only)
script append / replace-range  -> [array]  heading, action, dialog, duration_sec,
                                          order_index, location_id, characters[]
script update                   -> flags    --scene-id --heading --action --dialog
                                          --duration-sec --order-index --location-id
clips append / replace-range   -> [array]  description, shot_size, duration_sec,
                                          order_index, dialog_lines_covered[]
clips update                    -> flags    --clip-id --description --shot-size
                                          --duration-sec --order-index --dialog-lines-covered
context set                    -> [object] overview{}, requirements{}
project set                    -> flags    --title --idea --idea-file --genre --tone
                                          --target-duration --status --cover-path
```

`update` verbs take flags, not a payload file. The others take `--file`, and
`--file -` reads stdin if you already hold the JSON.

- What a write returns: `story append` → `beat_ids`, `script append` →
  `scene_ids`, `clips append` → `clip_ids`, `cast upsert` → `results`, one
  `{id, name, kind, action}` per entry where `action` is `created` / `updated` /
  `unchanged`. Only `cast upsert` tells you whether anything actually changed,
  because it is the only one that is an upsert by name.
- `--dialog-lines-covered` is comma-separated: `--dialog-lines-covered 1,3,7`.
- `--entity-id` is a row id **or the exact name**. Renaming is not supported —
  `name` is the upsert identity, and changing it would orphan the scene's
  references to that character.
- `cast list` comes back grouped by table — `{"characters": [...],
  "locations": [...], "items": [...]}` — and each row carries its own `kind`.
  `script list` does *not* include a scene's `characters`; read one scene with
  `script get --scene-id N`, which nests its `clips` under `clips`.
- `--portrait-path` / `--sheet-path` / `--consistency-prompt` point at images the
  user generated in the web UI. The CLI has no renderer and will not invent them;
  the only accepted value is `""`, to clear a stale reference.
- `script update` cannot change which characters are in a scene. `characters` is
  settable only when the scene is created — use `script replace-range` on that
  scene if the cast actually changed.

### `shot_size` is a closed list, exact spelling

`wide` · `medium` · `closeUp` · `insert` · `overShoulder`

`closeup` is **rejected**, not normalised. The CLI does not guess which of two
camelCase spellings you meant, because a whitelist that later gains a value
differing only by case would make the guess silently wrong. `""` is the one
accepted empty form — it means "unset".

### Fields you cannot write

`clip_path`, `workflow_id`, `video_settings_json`, `chain_from_prev`,
`video_node_id`, `render_status`, `portrait_path` (from a payload), and every
bookkeeping column a payload has no business naming — `id`, `project_id`,
`created_at`, `scene_id` — are rejected with exit 2. Those belong to the render
pipeline or to the CLI's own addressing. If you need one changed, the user does
it in the web UI.

`order_index` is the awkward one: `schema show` lists it for every group, but
only **beats** honour it. Scenes and clips take it and overwrite it with a dense
append order. See the two sections below before sending one.

## If another skill did the novel conversion

The common shape of this work: some third-party skill turns the novel into an
intermediate artifact, and you then write that into Calliope. Fully supported.
One rule covers it:

> **The intermediate artifact's shape is your problem. Calliope's shape belongs
> to `schema`.**

That skill may emit markdown, its own JSON, prose — anything. The CLI has never
heard of its format and will not try to. Ask the CLI for the shape, then convert
in your own context:

```bash
# 1. ask what the shape is -- never infer it from the upstream output,
#    and never reverse-engineer it from what `get` happens to return
calliope-cli schema show story replace-range
calliope-cli schema validate --group story --verb replace-range --file beats.json

# 2. fingerprint the rows you are about to overwrite
calliope-cli project hash --project 7 --json

# 3. dry-run first: it prints both sides plus the invariant preflight
calliope-cli story replace-range --project 7 --from-index 1 --to-index 999 \
    --file beats.json --expect-hash <story_beats hash> --dry-run

# 4. same line without --dry-run
```

**Do not hand the upstream artifact to `--file` unchanged.** Four habits it
reliably has are all caught, and catching them is the design working, not an
obstacle to route around:

| Upstream habit | What the CLI says |
| --- | --- |
| a character name that is not in the cast | exit 2, naming it: `unknown character '安'; write it with \`cast upsert --kind character\` first` |
| `"closeup"` | exit 2 — the list is closed and case-exact (see above) |
| `dialog_lines_covered` as `"12,14"` | exit 2 — it is a list of ints |
| `id` / `project_id` / `created_at` in the payload | exit 2 — those are the CLI's to set |

So when a write comes back exit 2, **fix the conversion, not the CLI.** There is
no flag to loosen validation and there will not be one: a payload that reached
the database half-converted would look like a success that silently did nothing.

**One upstream habit gets through, so watch it yourself: `order_index`.** It is
listed in `schema show` for every group, so an artifact that carries positions
looks perfectly valid — and the three groups then disagree about what it means:

| Group | an explicit `order_index` | what tells you |
| --- | --- | --- |
| `story` (beats) | **honoured**, stored as sent | `story order` reports the hole afterwards |
| `script` (scenes) | **overwritten** with a dense append order | nothing |
| `clips` | **overwritten** with a dense order within the scene | nothing |

An upstream artifact numbered `1, 3, 7` therefore becomes three beats at `1, 3,
7` with holes `2, 4, 5, 6` — not a rejection, and not what a dense list would
have been. After writing beats, run `story order --project 7 --json` and read the
holes and duplicates. For scenes and clips the safest move is to strip
`order_index` from the artifact during conversion and let append order speak.

**Compatibility is a property of the CLI boundary, not of this skill.** Whatever
the upstream produced — valid, nearly valid, or prose — either it validates or it
never enters the database.

**Do not route `[CARRY]` constraints through the third-party skill.** It rewrites
descriptions, reorders, and "improves" copy; constraint lines do not survive
that. Append them *after* conversion, or accept that upstream has no idea the
constraint exists.

One thing the CLI being used instead of the built-in generators means: those
generators **refuse** a source text over 2,000 characters (exit 422), because
they would spend a real call on text they cannot use and return beats that
quietly ignore it. Nothing here refuses — writing a novel's worth of beats is
the point.

## Per-clip continuity: the `[CARRY]` block

`context set` writes only two things — `overview` and `requirements`. It
deliberately cannot write per-shot continuity, because
`ensure_continuity_plan` rebuilds the whole plan whenever the board moves and
would discard anything stored there.

Per-clip constraints go in `clips.description` instead, after a `[CARRY]` marker
line:

```json
[{
  "description": "**closeUp.** Ann's hand closes around the lantern.\n[CARRY] The lantern stays lit from here on.\nHer left glove is missing from the second shot onward.",
  "shot_size": "closeUp",
  "dialog_lines_covered": [12]
}]
```

The block is **everything from the marker line to the end of the description** —
a constraint is a paragraph, not a sentence, so it is never truncated. Everything
before the marker is the shot description proper.

Read them back with:

```bash
calliope-cli shots list --project 7 --carry --json
calliope-cli clips list --project 7 --scene 3 --json
```

## Ordering and renumbering

`script append` and `clips append` renumber to a dense `1..N`. There are no gaps
and `order_index` always matches position, so you never have to compute it — and
if you send one anyway, it is overwritten rather than honoured.

- `replace-range --from-index A --to-index B` replaces that inclusive slice and
  renumbers. An **empty** replacement is rejected — use `update` to delete a row.
- `clips` are scoped by `--scene`, which takes a position or an id.

Every scene already has one auto-generated clip with an empty description, so
"the scene has clips" does not mean "the scene is written". `plan next` counts
that correctly and hands you the scene ids in `targets`.

### Beats are the one exception

Scenes and clips are forced dense. **Beats are not**, because the web UI is also
allowed to leave a gap and a half-written beat list is normal mid-project. A beat
may also *land* on a position you chose — `order_index: 9` in a `story append`
payload is stored as 9, and the next untargeted beat follows it to 10, so one
invented number leaves `2..8` missing.

So `story list` can legitimately come back with `1, 9, 10`, and `story append`
extends from the tail, which means the gap does not heal itself.

```bash
calliope-cli story order --project 7 --json
```

```json
{
  "counts": {"rows": 3, "distinct": 3, "min": 1, "max": 4},
  "dense": false,
  "duplicates": [],
  "holes": [3]
}
```

`duplicates` is the dangerous one: two rows claim the same `order_index`, reads
order by `(order_index, id)`, so you get an order you did not write. `plan next`
reports both in its `note` when it finds them — read that note before using a
position in `replace-range`, because "replace beat 3" means something different
when no beat 3 exists.

## `context set` and staleness — two different flags

```bash
calliope-cli context get --project 7 --json
```

| field | means |
|---|---|
| `authored` | the ledger has content in `overview` / `requirements` — **your** half |
| `stale` | the *derived* `shots` no longer match the board — the model's half |
| `basis_hash` | the board as it is right now |
| `stored_based_on` | what the derived plan was computed from |

`context set` does **not** reset `based_on`, and it must not: `based_on` is the
model's record of what it computed `shots` from. Blessing it would make the web UI
trust a per-shot plan that no longer matches the script. Only
`ensure_continuity_plan` can move it, and it needs the model.

So do not read `stale: true` as "my work is unfinished" — it will stay true
forever through the CLI. `plan next` gates on `authored` instead, which is why it
reaches `render` instead of asking you to rewrite the ledger on every pass. When
it gets there with a stale plan it says so in `note`, and that is a web UI action.

Writing a novel in? Its exact bytes matter, so they are stored verbatim:
`project set --idea-file` does not strip whitespace, and `project source` reports
`chars` for the *whole* text regardless of the window you read.

## When a write is refused

| exit | meaning | do this |
|---|---|---|
| 2 | your payload is wrong | read stderr, it names the field; `schema validate` first |
| 3 | **the content moved since you read it** | re-read, re-apply on top of what is there now |
| 4 | no such project / row | check the id with `project list` |
| 5 | policy refusal | should be unreachable from here; if you hit it, stop and report it |

Exit 3 is not a retry with the same payload. Someone edited that row — probably
the user in the web UI. Re-read, merge, write again.

Drift is reported per table, so `projects` moving tells you nothing about
`story_beats`. Check the table named in stderr.

## Audit log

Every write is recorded with before-images, in the same transaction as the change
itself. Append-only; there is no prune verb.

```bash
calliope-cli log list --project 7 --limit 20 --json
calliope-cli log list --command-filter "story append" --json
calliope-cli log show --entry-id 412 --json      # full before/after
```

Use it to answer "what did you change", and to find out whether a row you thought
was stale was changed by you or by the user.

## Boundaries — these are not configurable

- **The CLI cannot delete a project.** There is no `project delete`; there is no
  flag that enables one. If asked to, say the web UI has to do it.
- **The CLI never calls a model.** Nothing in `cli/` can reach `llm.generate` or
  `ensure_continuity_plan`. If you want generation, that is the web UI.
- **No images, no video.** Those columns are read-only to the CLI.
- **Writes are validated against a closed field list.** Unknown keys are a hard
  error, not ignored — a typo'd field that was silently dropped would look like a
  successful write that did nothing.

## The full loop

```bash
# 0. where am I?
calliope-cli project list --json
calliope-cli plan next --project 7 --json

# 1. what does it want? what shape does it take?
calliope-cli schema show story append

# 2. check my payload before writing 40 beats
calliope-cli schema validate --group story --verb append --file beats.json

# 3. fingerprint, then read what is actually there
calliope-cli project hash --project 7 --json
calliope-cli story list --project 7 --json

# 4. write, guarded
calliope-cli story append --project 7 --file beats.json \
    --expect-hash 4f2a9c1e8b7d6053

# 5. confirm, and see what landed
calliope-cli story list --project 7 --json
calliope-cli log list --project 7 --limit 1 --json

# 6. next?
calliope-cli plan next --project 7 --json
```

`--dry-run` on any write prints both sides without touching anything. Use it when
a batch is large or the target is not obvious.

## Invocation

`calliope-cli` is `calliope-cli.bat` at the repo root. From PowerShell use
`.\calliope-cli.bat`; from cmd, `calliope-cli.bat`. It sets the code page to UTF-8
itself, so Chinese in `--idea` or a payload file survives the round trip.

The project is resolved from the backend config, so the CLI and a running web UI
point at the same database. That is intentional — it is what makes the drift
guard meaningful — and it is also why you must read immediately before writing.