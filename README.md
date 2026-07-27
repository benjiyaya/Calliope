# Calliope

Calliope is a local-first story-to-video studio. You type a story idea; Calliope drafts a storyline with beats, characters, and locations, writes a per-scene script, then generates a video clip per scene by driving your own ComfyUI install. When the clips are done, one click stitches them into a finished film with crossfades and matched loudness (ffmpeg). Everything runs on your machine: projects live in SQLite, media lives in folders, and no cloud service is involved beyond the LLM endpoint you point it at.

## Download & run (Windows)

1. Grab **`Calliope-win-x64.zip`** from the [latest release](../../releases).
2. Unzip it anywhere writable (avoid `Program Files`).
3. Run `win-unpacked\Calliope.exe`.

No installer, no dev setup. First launch shows a splash while the backend boots. Windows SmartScreen will warn on first run (unsigned binary) — that's expected.

**Portable:** your projects, database, generated media, and settings live next to the app:

```text
<app>\resources\backend\data\                 projects database + generated assets
<app>\resources\backend\calliope_config.json  created when you save Settings
```

Move or copy the whole unzipped folder to relocate the app together with its data.

<img width="1702" height="1041" alt="Screenshot 2026-07-28 045615" src="https://github.com/user-attachments/assets/e5450a46-d8f9-48fe-8163-5efffaeccb0e" />

<img width="1702" height="1049" alt="Screenshot 2026-07-28 052215" src="https://github.com/user-attachments/assets/703c3def-c992-41f7-8fef-bb2c7ef51535" />

<img width="1697" height="1047" alt="Screenshot 2026-07-28 052242" src="https://github.com/user-attachments/assets/84618fe3-1f33-4482-bbe1-b8d13866bd96" />

<img width="1686" height="967" alt="Screenshot 2026-07-28 052349" src="https://github.com/user-attachments/assets/193af234-3862-467b-9087-373ffcd26d4a" />


## Requirements

- **Windows 10/11**
- **A running ComfyUI install**, with the models your workflows need already set up
- **An OpenAI-compatible LLM endpoint** — local (LM Studio, Ollama, etc.) or hosted
- **ffmpeg on PATH** — needed for film export

## First run

Open the app, go to **Settings**, and set:

1. **LLM** — base URL, model name, and API key of your OpenAI-compatible endpoint
2. **ComfyUI** — the base URL of your running ComfyUI (e.g. `http://127.0.0.1:8188`)

Leave **Dry-run** off — it is meant for testing and produces placeholder results instead of real generations.

## Using the app

The app walks a project through four stages — **Story, Assets, Script, Video**:

- **Story:** describe your idea and generate a draft storyline with beats, characters, and locations. Generating again replaces the current draft — the UI asks for confirmation first. Edit anything by hand before moving on.
- **Assets:** each character and location has its own **Image prompt**. Pick a workflow and shared settings (width/height/etc.) at the top, then click Generate per entity to produce reference images on your ComfyUI. Regenerate any single entity without touching the others.
- **Script:** generate (or regenerate) the per-scene script. Scenes link back to the characters and locations from the Story stage.
- **Video:** each scene gets a **Generate** button that queues a clip job on ComfyUI with the right prompt and reference images. Clips save to local disk and appear in the scene timeline.
- **Film view:** once scenes have clips, **Export film** stitches them with ffmpeg: every clip is normalized to 1080p30, joined with 0.5s crossfades, and loudness-normalized into one final file.
- When everything is done the project is automatically marked **Completed**.

**Playground** is a free-form generation page outside the project pipeline: run any imported workflow with arbitrary inputs, upload your own files (image / video / audio) as inputs, and optionally attach a result to a project as an asset.

## ComfyUI workflows (important)

Calliope does **not** hardcode Comfy node IDs. It discovers editable nodes from **role tags** in the node titles of an **API Format** workflow JSON. The `ComfyAPI/` folder in this repo contains ready-to-import example workflows (LTX-2.3 ref2vid, krea2 t2i).

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
| `image` | `img` | Generic image input |
| `seed` | — | Shared form |

Output roles:

| Role | Aliases |
|---|---|
| `image` | `img` |
| `video` | `vid` |

Unknown roles still show up in the dynamic form; they just get no special auto-fill. Plain `(Input)` / `(Output)` without a role still works through a deprecated label fallback, so old workflows keep working — but tag new workflows with explicit roles.

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

## Source code

The app source (FastAPI backend, SvelteKit frontend) is not published yet — this repo currently ships the packaged app (via Releases), the Electron shell (`electron/`), and example ComfyUI workflows (`ComfyAPI/`).
