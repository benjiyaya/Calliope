# 🎬 MiniMax H3 Video Prompt Agent Skill

An agent skill (DSH / harness / file-based agents) that transforms rough video ideas + reference images/audio into production-grade **MiniMax H3** video generation prompts.

## What It Does

When you attach images, video, or audio and describe a video concept, this skill:

1. **Detects the H3 mode** automatically (Ref2VA, T2VA, I2VA, FL2VA, L2VA)
2. **Creatively enhances** your idea across 7 cinematic dimensions
3. **Outputs the exact H3 format** — ready to paste into ComfyUI

## H3 Modes Supported

| Mode | Use Case | Output Format |
|---|---|---|
| **Ref2VA** | Character/style/voice reference assets | 6-section structured prompt |
| **T2VA** | Text-to-video (no images) | Timed multi-shot timeline |
| **I2VA** | 1 image as first frame | Instruction line + timeline |
| **FL2VA** | 2 images (first + last frame) | Instruction line + timeline |
| **L2VA** | 1 image as last frame | Instruction line + timeline |

## Creative Enhancement Dimensions

Every prompt is enriched across:

- 🎥 **Camera Identity** — Physical camera type, imperfections, format aesthetic
- 🎨 **Visual Texture** — Grain, color palette, lighting design, exposure behavior
- ⚡ **Pacing Arc** — Energy progression, cut rhythm, build patterns
- 👤 **Character Detail** — Physical features, wardrobe, visual signature colors
- 🧭 **Spatial Geography** — Screen directions, action vectors, environmental layout
- 🔄 **Continuity Progression** — State tracking, damage accumulation, emotional arcs
- 🔊 **Sound Design** — Ambience, SFX, diegetic/non-diegetic music mapping

## Installation

### For DSH / file-based agents (this skill)

Place the `h3-video-prompt-enhancer/` folder in your agent's skills directory so it auto-loads by name. In DSH the skill is invoked by its exact name (`h3-video-prompt-enhancer`); the harness injects `SKILL.md` as instructions when the task matches, and the `references/` files are loaded on demand via the read tool.

```
skills/
└── h3-video-prompt-enhancer/
    ├── SKILL.md
    └── references/...
```

Restart / refresh the agent's skill catalog if it does not appear. The skill auto-triggers when you attach media and describe a video idea.

### For Any File-Based Agent (Claude, Codex, Hermes, etc.)

The `SKILL.md` and reference files are standard Markdown — load them as system context or project rules in any agent that supports file-based instructions.

## File Structure

```
h3-video-prompt-enhancer/
├── README.md                              # You are here
├── SKILL.md                               # Main skill — workflow, rules, verification
└── references/
    ├── ref2va-format.md                   # Full Ref2VA 6-section format spec
    ├── base-multishot-format.md           # T2VA / I2VA / FL2VA / L2VA format spec
    └── creative-showcase.md               # Advanced long-form prompt patterns & benchmarks
```

## Usage Examples

### Ref2VA (Character References)

```
📎 Attach: character_sheet.png, voice_sample.mp3
💬 "A woman matching this character walks through a neon-lit Tokyo street at night, 
    stops at a ramen stand, says hi to the cook"
```

→ Outputs a 6-section structured prompt with `subject_definitions`, `summary`, `retention_analysis`, `detailed_description`, `overall_soundscape`, `non_diegetic_music`.

### I2VA (Image-to-Video)

```
📎 Attach: first_frame.png
💬 "A baker opening his shop before sunrise, proud of the first loaf"
```

→ Outputs a timed multi-shot timeline anchored to the first-frame image.

### FL2VA (First + Last Frame)

```
📎 Attach: first_frame.png, last_frame.png
💬 "Day to night transition over a city skyline"
```

→ Outputs a continuous interpolation prompt from first to last frame.

## Key Format Rules

- Output is **raw structured text** — no markdown fences, no preamble
- All timestamps use `MM:SS.mmm` format, strictly increasing
- One dominant action per shot — never cram multiple actions
- Camera motion written as natural English (e.g., *"The camera pushes in with small amplitude at slow speed"*)
- Dialogue preserved verbatim inside `<d>[Language] ...</d>` tags
- Reference labels (`<Subject N>`, `<Picture N>`, `<Video N>`, `<Audio N>`) stay consistent across all sections

## ComfyUI Integration

```
LLM Node (with system prompt) → text output → H3 Ref2VA / H3 Base node prompt input
```

Keep the ASSETS list order identical to the physical wiring order of the reference inputs — labels map positionally (Image 1 → first image socket, etc.).

## Provenance

Built for the [MiniMax H3](https://github.com/MiniMax-AI) omni-modal video model. The Ref2VA and Base MultiShot format specs are derived from MiniMax's official H3-Context-IR preprocessor documentation.

## License

MIT

## Author

**Benjamin Law** — [Muse-AI](https://muse-ai.io)
