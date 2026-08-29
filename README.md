# Calliope

Calliope is a local-first story-to-video studio. You type a story idea; Calliope drafts a storyline with beats, characters, and locations, writes a per-scene script, then generates a video clip per scene by driving your own ComfyUI install. When the clips are done, one click stitches them into a finished film with crossfades and matched loudness (ffmpeg). Everything runs on your machine: projects live in SQLite, media lives in folders, and no cloud service is involved beyond the LLM endpoint you point it at.


<img width="1672" height="1015" alt="Screenshot 2026-08-21 033244" src="https://github.com/user-attachments/assets/57bc3d05-f33e-415c-9ce9-e7d796e3bcdd" />

<img width="1508" height="1131" alt="Screenshot 2026-08-23 192042" src="https://github.com/user-attachments/assets/61cb10fb-a8a2-4096-beff-e504a8f7c8df" />

<img width="1847" height="1177" alt="Screenshot 2026-08-23 231317" src="https://github.com/user-attachments/assets/4c1ccac3-39f0-4704-a129-6fc5b5039415" />



## Install — from source (npm + Python)

**Prerequisites**

- Python 3.11+
- Node.js 18+ (npm)
- A running ComfyUI install, with the models your workflows need already set up
- An OpenAI-compatible LLM endpoint — local (LM Studio, Ollama, etc.) or hosted
- ffmpeg on PATH — needed for film export

**1. Backend (FastAPI)**

```bash
cd calliope-backend
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m calliope.main --host 127.0.0.1 --port 8247
```

Optionally copy `calliope_config.example.json` to `calliope_config.json` and edit it before starting (LLM endpoint, ComfyUI URL). You can also configure everything later in the app's **Settings** page. Never commit `calliope_config.json` — it stores your API key.

**2. Frontend (SvelteKit)**

```bash
cd calliope-web
npm install
npm run dev
```

Open `http://127.0.0.1:5173`. The dev server proxies `/api` to the backend on `127.0.0.1:8247`.

## First run

Open the app, go to **Settings**, and set:

1. **LLM** — one or more OpenAI-compatible endpoints (base URL, model name, API key). Save several, then pick which one is **Active**.
2. **ComfyUI** — the base URL of your running ComfyUI (e.g. `http://127.0.0.1:8188`)
3. **Agent** *(optional)* — assign a specific LLM per agent role (see below)

Leave **Dry-run** off — it is meant for testing and produces placeholder results instead of real generations.

### Queue settings

In **Settings → Queue** you can tune how the worker talks to ComfyUI:

- **Concurrency** — how many jobs run at once.
- **Poll interval** — how often Calliope checks ComfyUI for a finished job.
- **Poll timeout (seconds)** — how long Calliope keeps waiting on ComfyUI for a single job before failing it. **Default is `1800` (30 minutes).** Long video generations can easily exceed 10 minutes, so raise this for heavy workflows — or set it to **`0` to wait indefinitely** (until the job finishes or you cancel it).
- **Max retries** — automatic retries before a job is marked failed.

### Agent settings

In **Settings → Agent**:

- **Model per agent** — assign any saved LLM to each role: **Main agent, Planner, Story agent, Script agent, Assets agent, Video agent**. Blank means the **Active LLM** from Settings → LLM applies, so a single-endpoint setup needs no configuration here. A common setup is a strong cloud model for the main agent and planner, and a fast local model for the sub-agents. The Video agent's assignment also drives the MiniMax H3 prompt rewrite.
- **System-prompt rules (hardening)** — operator rules appended to every agent system prompt. Leave blank to disable.

## Using the app

The app walks a project through four stages — **Story, Assets, Script, Video**:

- **Story:** describe your idea and **Draft Storyline** — this opens a project-linked chat in the Agents view with the prompt pre-filled, and the agent writes beats, characters, locations, and misc. items. Edit anything by hand before moving on.
- **Assets:** each character, location, and item has its own **Image prompt**. Pick a workflow and shared settings (width/height/etc.) at the top, then click Generate per entity to produce reference images on your ComfyUI. Regenerate any single entity without touching the others.
- **Script:** **Regenerate Script** also opens a project-linked Agents chat (pre-filled) to rewrite the per-scene script. Scenes link back to the characters and locations from the Story stage.
- **Video:** each scene gets a **Generate** button that queues a clip job on ComfyUI with the right prompt and reference images (plus optional video/audio file refs). Scenes marked **Continue from previous video** in Script extend the previous clip instead of cutting fresh — see [Continue from previous clip (video extend)](#continue-from-previous-clip-video-extend).
- **Film view:** once scenes have clips, **Export film** stitches them with ffmpeg: clips are normalized to 1080p at the **majority frame rate of the clips themselves** (24 fps clips export at 24 fps; mixed-rate projects conform to whichever rate most clips use), joined with 0.5s crossfades, and loudness-normalized into one final file.
- When everything is done the project is automatically marked **Completed**.

**Playground** is a free-form generation page outside the project pipeline: run any imported workflow with arbitrary inputs, upload your own files (image / video / audio) as inputs, and optionally attach a result to a project as an asset.

**Agents** is a chat-driven way to run the same pipeline: talk to a production agent that operates Calliope through tools (create project, draft story, write script, queue asset/video renders, watch jobs). Every chat session is bound to at most one project — start a **Sandbox** chat with no project and the agent materializes one via `create_project`, linking the session automatically; or link a session to an existing project and ask for edits. Complex builds are decomposed by a planner into sub-agents (story → script → assets → video). Everything the agent does goes through the same database and render queue the project UI reads — nothing bypasses the normal pipeline. When the agent waits on renders (`wait_for_jobs`), it uses the same **Poll timeout** as the queue worker (default 30 minutes).

## ComfyUI workflows (important)

Calliope does **not** hardcode Comfy node IDs. It discovers editable nodes from **role tags** in the node titles of an **API Format** workflow JSON. The `example_ComfyUI_workflows/` folder in this repo contains ready-to-import examples (MiniMax H3 reference-to-video, krea2 text-to-image, character sheet).

### 1. Tag your nodes in ComfyUI

Rename the input/output nodes so their titles carry a role tag:

```text
Display Name (Input:role)
Display Name (Output:role)
```

The display name can be anything, in any language. The `:role` part is the contract. Examples:

```text
Main Prompt (Input:prompt)
Neg (Input:negative)
W (Input:width)
H (Input:height)
Char Ref (Input:character)
Env Ref (Input:location)
Result (Output:image)
Clip (Output:video)
```

### 2. Canonical roles

Input roles:

| Role | Aliases | Filled by |
|---|---|---|
| `prompt` | `positive` | Entity Image prompt (Assets), scene/job prompt |
| `negative` | `neg` | Negative prompt when provided |
| `width` | `w` | Shared form / defaults |
| `height` | `h` | Shared form / defaults |
| `character` | `char`, `portrait`, `sheet`, `face`, `ref` | Character reference path |
| `location` | `loc`, `environment`, `env`, `background`, `scene` | Location reference path |
| `image` | `img` | Generic image input (ordered ref slot — see below) |
| `video` | `vid` | Video file input (`LoadVideo`) |
| `audio` | `sound`, `sfx` | Audio file input (`LoadAudio`) |
| `seed` | — | Shared form |
| `duration` | `dur`, `length`, `seconds` | Scene duration (video jobs) |

Output roles:

| Role | Aliases |
|---|---|
| `image` | `img` |
| `video` | `vid` |

Unknown roles still show up in the dynamic form; they just get no special auto-fill. Plain `(Input)` / `(Output)` without a role still works through a deprecated label fallback, so old workflows keep working — but tag new workflows with explicit roles.

#### Prompt profiles

Each workflow has a **Prompt format** setting (default *Plain prose*). When set to *MiniMax H3 reference (6-section)*, Calliope rewrites each scene prompt at generation time into MiniMax H3's full-reference format (`subject_definitions` → `summary` → `retention_analysis` → `detailed_description` → `overall_soundscape` → `non_diegetic_music`, with `<Subject N>` labels and `<d>[Language] …</d>` dialogue). It is auto-suggested on import when the workflow contains a `MiniMaxH3*` node.

For multi-reference workflows, generic `(Input:image)` inputs are filled in **node-id order** — characters in scene order, then the location — and that order defines the `<Subject N>` numbering in the prompt. Keep your ref node ids in the order you want subjects numbered. A `(Input:duration)` node receives the scene's duration in seconds.

**Using the H3 profile from the scene form (Generate clip):** the form's auto-fill vs. override rule is simple — anything you type or pick wins over the automatic value.

- **Text Prompt — leave it empty.** An empty field gets the LLM-rewritten six-section H3 prompt built from the scene's action, dialogue, characters, and location. If you type anything, your text is sent verbatim and the model receives plain prose instead of the H3 format.
- **Ref 1 / Ref 2 — leave them on "Choose asset…"** to auto-fill from the scene's characters (in scene order) then the location, with `<Subject N>` numbering matched to those slots. Picking an asset manually overrides just that slot (and you take over subject numbering for it).
- **Duration** auto-fills from the scene's estimated duration; edit it only when you want a different clip length.

### Continue from previous clip (video extend)

Long takes don't have to be one giant generation. Mark a scene **Continue from previous video** in the **Script** stage and instead of cutting a fresh clip, it extends the previous scene's clip as real continuation footage (the first scene can't use the toggle).

The **Video** stage enforces one requirement: the scene's workflow must have an input tagged `(Input:video)` (a `LoadVideo` node). Continue scenes on a workflow without one have Generate disabled with a warning.

When the workflow qualifies, a **clip source picker** appears on the continue scene:

- **Auto** (default) — the previous scene's clip is used, resolved when the job actually runs.
- **Upload file** — extend from any video you provide (a Playground upload).
- **From timeline** — pick a specific earlier scene's clip explicitly.

Auto is safe even when scenes are queued in one batch: Calliope's queue renders one job at a time, so by the time a continue scene runs, the scene before it has already rendered and its clip is picked up automatically.

The workflow pattern (per [kat3ri/ComfyUI-MiniMax-H3-Extend](https://github.com/kat3ri/ComfyUI-MiniMax-H3-Extend)) is a `LoadVideo (Input:video)` node feeding the MiniMax H3 extend patched nodes (`MiniMaxH3EncodeAVPatched` → `MiniMaxH3VideoExtendPatched`) with the `(Output:video)` node at the end. Recommended starting settings from that repo: `context_frames` **2**, `ref_spacing` **1–2**, `ref_decay` **0.3**, `ref_ramp` **3–4** (5–6 if the prior clip had heavy motion).

### 3. Export the workflow

In ComfyUI, use **Save (API Format)** — not the regular UI workflow graph format. Calliope only understands API Format JSON.

### 4. Import into Calliope

Settings → Workflows → import the JSON → **analyze** → check the preview shows the expected **role** next to each input → save → enable the workflow where you want to use it (Assets, Playground, per scene).

### 5. Troubleshooting

If ComfyUI "doesn't know what to generate" or jobs come back empty:

- The workflow title must be literally `(Input:prompt)` — not only `(Input)` — for prompts to land reliably.
- Check the job payload: `input_values` for that node must be non-empty (blanks are stripped before submission).
- Make sure **Dry-run** is off in Settings (default is off).
- An unreachable ComfyUI fails the job honestly — Calliope never silently fakes images. Check the base URL and that ComfyUI is running.

### 6. HTTP only

Calliope talks to ComfyUI purely over its HTTP API: it uploads reference files with `POST /upload/image`, patches the workflow JSON and queues it via `POST /prompt`, polls `/history/{prompt_id}`, and downloads the results. It **never reads or writes ComfyUI's local `input/` / `output/` folders** — any folder paths stay configured on the ComfyUI side, not in Calliope.

## License

This project is licensed under the [MIT License](LICENSE) 

## Repo layout

```text
calliope-backend/            FastAPI backend (Python)
calliope-web/                SvelteKit frontend
example_ComfyUI_workflows/   ready-to-import API-format workflow JSONs
docs/wiki/                   design notes (wiki source): ComfyUI HTTP vs MCP, multi-ref workflows
```

