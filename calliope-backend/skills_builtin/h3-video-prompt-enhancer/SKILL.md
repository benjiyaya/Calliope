---
name: h3-video-prompt-enhancer
description: "Use when making MiniMax H3 video prompts from media + ideas."
version: 1.1.0
license: MIT
metadata:
  author: Benjamin Law (Muse-AI)
  tags: [video, prompt-engineering, minimax-h3, comfyui, text-to-video, image-to-video, creative]
  related_skills: [comfyui]
---

# H3 Video Prompt Enhancer

## Overview

Transform a user's rough video idea + attached assets into a production-grade MiniMax H3 video generation prompt. The skill handles two H3 generation modes:

- **Ref2VA** — when the user provides reference assets (character sheets, style images, reference videos, voice samples). Outputs a 6-section structured prompt. Load `references/ref2va-format.md` for the full spec.
- **Base MultiShot** (T2VA / I2VA / FL2VA / L2VA) — for text-to-video or image-anchored generation. Outputs a timed multi-shot prompt. Load `references/base-multishot-format.md` for the full spec.

The skill's distinctive value: it doesn't just format-comply — it **creatively enhances** the brief with professional-grade cinematic detail (camera aesthetics, visual texture, lighting design, pacing arcs, spatial choreography, continuity tracking) before mapping everything into the exact H3 output format. Load `references/creative-showcase.md` for quality benchmarks and pattern examples from advanced long-form prompts.

## When to Use

**Trigger when the user:**
- Attaches 1+ images/videos/audio files and describes a video they want to create
- Says "make a video prompt" / "enhance this for H3" / "write a Ref2VA prompt" / "img2vid" / "txt2vid"
- Provides a video concept and wants it structured for MiniMax H3 generation
- Mentions H3, MiniMax, Ref2VA, ComfyUI video generation
- Pastes a creative brief (CAMERA/LOOK/STYLE format) and wants it converted to H3 format

**Don't use for:**
- Non-H3 video models (Sora, Runway, Kling, etc.) — the output format is H3-specific
- Pure image generation prompts
- Video editing tasks that don't involve new generation

## Step 0: Classify the Mode

Determine the H3 mode from what the user attached and stated:

| User provides | Mode | Format reference |
|---|---|---|
| Reference images/videos/audio (character sheets, style refs, voice clips) | **Ref2VA** | `references/ref2va-format.md` |
| Nothing — just a text idea | **T2VA** (text-to-video) | `references/base-multishot-format.md` |
| 1 image as the first frame | **I2VA** (image-to-video) | `references/base-multishot-format.md` |
| 2 images (first frame + last frame) | **FL2VA** (first-last-to-video) | `references/base-multishot-format.md` |
| 1 image as the last frame only | **L2VA** (last-frame-to-video) | `references/base-multishot-format.md` |

**Key distinction:** An image used as a *first/last frame anchor* = I2VA/FL2VA/L2VA. An image used as a *character/style reference* (not a frame position) = Ref2VA. When ambiguous, ask the user: "Is this image a frame anchor (first/last frame of the video) or a reference (character/style/scene)?"

### Reading the Assets (DSH)

In DSH you must actually *see* or *transcribe* each reference asset before you can describe it faithfully. Two requirements apply:

- **Vision capability.** The active model must accept image input. Use the harness image tool on each sheet — if the tool reports `model ... does not declare image input`, the current model is not vision-capable. Switch to an image-capable model (or delegate the visual read to a vision-capable partner) before describing reference images. Do NOT guess a character's appearance from a filename.
- **Asset description depth.** Character sheets usually show multiple views (front/side/back, several poses). Extract per view: identity (gender, age, build), face (hair, skin, eyes, markings), full outfit head-to-toe with exact colors/materials, weapons/props, and any on-screen labels. This feeds `subject_definitions`.
- **Voice/audio assets.** Transcribe or listen to each clip to capture the speaker's timbre, pitch, rate, and accent for the `(Sx)` speaker IDs. If speech transcription is unavailable in the harness, note the delivery style from any user-provided description and keep `(Sx)` IDs generic.

## Step 1: Gather Parameters

Confirm these before enhancing (ask if missing, but proceed if the idea is clear enough):

- **duration_s**: 4–15 seconds (integer). Default to 8 if unspecified.
- **aspect ratio**: 16:9, 9:16, 1:1, 4:3, 21:9. Default to 16:9.
- **shot count**: Let the planner decide, or respect user's explicit count. Budget: 4–6s → 1–2 shots; 7–10s → 2–3 shots; 11–15s → 3–5 shots.
- **asset inventory**: What each attached file is and its role. Numbering per modality defines labels: Image k → `<Picture k>` or `<Subject N>`; Video k → `<Video k>`; Audio k → `<Audio k>`.

## Step 2: Creative Enhancement

This is the core value-add. Take the user's idea and enrich it across seven dimensions. The goal: produce a prompt with the depth and cinematic intelligence of a professional storyboard. Consult `references/creative-showcase.md` for full examples of each pattern.

### Enhancement Dimensions

**1. Camera Identity** — Assign a distinct camera aesthetic that matches the scene's tone:
- Physical type: handheld, tripod, drone, steadicam, dolly, security cam, dashcam, POV, arc shot
- Imperfections to preserve (when stylistically appropriate): hand tremor, autofocus hunting, exposure fluctuation, lens flare, motion blur, awkward zoom
- Format/aesthetic hint: 16mm film, DV tape, digital clean, anamorphic, vintage camcorder, broadcast

**2. Visual Texture (LOOK)** — Define the image quality and color science:
- Grain/noise: film grain, electronic noise, clean digital, VHS tracking, subtle blur
- Color palette: warm/cool, saturated/desaturated, high/low contrast, natural skin tones
- Lighting design: natural, studio, neon, golden hour, mixed sources, practical lights
- Lighting transitions: if locations change across shots, describe how the light shifts

**3. Pacing Arc** — Plan the energy progression across the full duration:
- Build patterns: quiet→energetic, tense→release, slow build→explosive peak→settle, steady rhythm
- Cut rhythm: accelerating cuts toward climax, slow contemplative holds, musical cutting on beats
- Match the pacing arc to the emotional intent of the brief

**4. Character Detail** — Flesh out every on-screen person with specificity:
- Physical: age range, build, hair color/style, skin tone, distinctive features (scars, freckles, heterochromia)
- Wardrobe: specific garments with colors, materials, textures, accessories — note changes across locations or time
- Visual signature: a recurring color or visual element that makes the character instantly recognizable in every shot (e.g., "electric purple energy trails", "glossy teal jacket reflections", "always wears a red scarf")
- Coverage note: describe outfits fully — avoid implying revealing clothing unless the user explicitly requests it

**5. Spatial Geography** — For action sequences or multi-location videos:
- Screen direction: who enters from where, movement vectors (Left→Right, Deep→Front, foreground↔background)
- Key action moments: the 2–3 critical motion beats that define the sequence
- Environmental layout: what's in the space, how it's lit, reflective surfaces, depth

**6. Continuity Progression** — Track what changes across shots so the video feels coherent:
- Physical state: damage accumulates, hair gets messier, clothes get wet/torn/dusty
- Environmental: props move, lights flicker, weather shifts, debris scatters
- Emotional: expressions and body language evolve naturally across the timeline

**7. Sound Design Plan** — Map the full audio landscape:
- Ambience: room tone, environmental atmosphere, background chatter, traffic, wind
- Physical action sounds: footsteps, impacts, fabric rustling, door creaks, liquid pouring
- Non-diegetic score: instrumentation, tempo, rhythm, dynamic changes (goes in `non_diegetic_music` field)
- Diegetic music: source-visible music — radio, speaker, live performance (goes in shot description)
- Dialogue vs. voiceover: clearly mark which is spoken on-camera vs. off-screen narration

### Per-Shot Quality Bar

Every shot in the storyboard must specify:
- **Composition**: framing (wide, medium, close-up, extreme close-up, macro), angle (eye-level, low, high, overhead, Dutch)
- **Camera motion**: type + amplitude + speed in natural English (e.g., "The camera pushes in with small amplitude at slow speed")
- **Subject action**: exactly ONE dominant action per shot — never cram multiple actions
- **Environment/lighting**: what's visible, how it's lit, time of day cues
- **Sound cue**: what's audible in this specific moment
- **Reference labels** (Ref2VA only): where referenced content appears or takes effect

## Step 3: Format and Output

Load the appropriate format reference and produce the final H3 prompt.

**For Ref2VA** → Load `references/ref2va-format.md`. Output exactly 6 sections in order:
1. `subject_definitions:` — one line per tracked item
2. `summary:` — task-type prefix + one paragraph
3. `retention_analysis:` — fidelity markers per label
4. `detailed_description:` — 350–500 words, opens with style, then `[Shot N]` timeline
5. `overall_soundscape:` — 1–4 sentences
6. `non_diegetic_music:` — 1–3 sentences or N/A

**For Base MultiShot** → Load `references/base-multishot-format.md`. Output:
1. Instruction line (I2VA/FL2VA/L2VA only — exact template from reference)
2. `integrated_multimodal_description:` — timed multi-shot timeline
3. `overall_soundscape:` — 1–4 sentences
4. `non_diegetic_music:` — 1–3 sentences or N/A

### Critical Format Rules (both modes)

- Output ONLY the specified fields — no preamble, no explanation, no markdown fences, no commentary
- Write everything in English. Exceptions: dialogue/lyrics inside `<d>` tags and visible on-screen text keep their original language verbatim
- Timestamps: `[Shot 1]` has NO timestamp and opens with style + initial composition. Later shots: `[Shot N] At MM:SS.mmm, the camera cuts to ...` with strictly increasing times within duration
- A cut must add NEW information (subject, space, state, viewpoint, time). If only distance/angle changes, use camera motion instead of a cut
- Camera motion = motion type + amplitude + speed as natural English action, not stacked labels
- Dialogue format: identifying phrase + speaker ID + delivery OUTSIDE `<d>`; inside `<d>` ONLY language tag + exact words: `<d>[English] Wait for us!</d>`
- Preserve the user's dialogue verbatim — never translate, rewrite, or paraphrase
- Voiceover: exact phrase "says in an off-screen voiceover" + immediately state "while his/her lips remain completely closed"
- On-screen text (signs, neon, subtitles): English double quotation marks, verbatim, no translation
- Avoid named third-party IP, real celebrities, trademarked characters — describe them generically

### Camera Motion Vocabulary

Types: Zoom In/Out, Push In/Pull Out, Pan L/R, Truck L/R, Tilt Up/Down, Pedestal Up/Down, Arc Shot, Tracking Shot, Static Shot, Shake Slightly/Strongly, POV, Roll CW/CCW.
Amplitude: "with small amplitude" / "with large amplitude" (omit when medium).
Speed: "at slow speed" / "at fast speed" (omit when normal).

## Step 4: Present to User

After generating the H3 prompt:
1. Present the full prompt in a code block so it can be copied directly
2. State which mode was detected and why
3. Flag any assumptions made (e.g., "Assumed 8s duration and 16:9 — adjust if needed")
4. Offer to refine specific aspects: camera aesthetic, pacing, character detail, shot count, sound design

## Creative Patterns from Showcase

The user maintains advanced long-form prompt examples representing the quality bar. Load `references/creative-showcase.md` for full examples. Key transferable patterns:

**Long-form Storytelling** (montage, day-in-the-life, narrative):
- Explicit CAMERA/LOOK/STYLE blocks defining the aesthetic before the storyboard
- Outfit and location progression across time, with lighting transitions
- Voiceover carrying emotional narrative over non-synchronized visuals
- Pace building from quiet opening to high-energy finale
- Per-shot storyboard with timing, action, and VO lines

**Action Choreography** (combat, chase, sports):
- Per-character color lock — each character has a signature color for their effects/trails/reflections
- Explicit spatial layout with screen directions (Deep→Front, Left→Right)
- Action vectors — the key motion moment that defines each shot
- Progressive continuity — damage accumulates, hair gets windblown, dust/cracks appear
- Strict shot count enforcement — state the count and hold it
- Environmental reactivity — holograms flicker, floors reflect, walls crack in response to action

## Anti-Slop: Motion Quality Checklist

"Slop" in H3 prompts means shots that look flat, static, or lifeless in the generated video. The most common cause is too many wide shots, static camera holds, and vague action descriptions. Before finalizing any prompt, verify every shot against these rules:

### Every Shot Must Have:
1. **A close-up or tight framing** — never a wide establishing shot during action beats. Extreme close-ups of hands, faces, weapons, impact points.
2. **ONE clear physical action** broken down at body-part level: fingers gripping, eyes tracking, feet grinding, hips rotating, jaw clenching. Not "she fights him" — describe the exact limb movement.
3. **Camera movement** — every shot needs explicit camera motion (type + amplitude + speed). No "camera holds" or "static shot" during action. Whip-pans, tracking, orbits, push-ins.
4. **No crammed actions** — if the brief describes 3 sequential actions, split across 3 shots with cuts. One action per shot is a hard rule.

### Slop Patterns to Avoid:
| Slop | Fix |
|---|---|
| Wide establishing shot during action | Cut to close-up of feet/hands/face at action onset |
| "Camera holds on the standoff" | Camera pushes in, orbits, or whip-pans to maintain motion |
| "She ducks, grabs, pivots, and throws" in one shot | Split into: duck (close-up) → grab (hand close-up) → pivot+throw (tracking) |
| "The camera follows them" (vague) | "The camera tracks at fast speed, whip-panning between striker and target" |
| Characters standing/walking with no action | Every beat needs a physical micro-action: eyes narrowing, fingers tightening, weight shifting |
| Static aftermath shot | Even aftermath needs camera movement: pull-back, tilt-up, slow orbit |

### Rewriting Slop:
When a prompt feels flat, identify the weakest shots and rewrite with this pattern:
1. **Zoom in** — change wide/medium to close-up or extreme close-up
2. **Add body-part detail** — "his fist" → "his white-knuckle fist, tendons visible"
3. **Add camera motion** — "the shot cuts to" → "the camera whip-pans to" or "tracks at fast speed"
4. **Split crammed actions** — extract one action per shot, add a cut between them
5. **Replace static moments** — "holds on the aftermath" → "pulls back slowly as..." or "orbits around..."

## Common Pitfalls

1. **Treating Ref2VA reference images as frame anchors.** A character sheet or style reference is a `<Subject>`, not a `<Picture>`. Only use `<Picture N>` standalone when the image IS a concrete frame position (first frame, keyframe, last frame). When in doubt, cite the image inside the relevant `<Subject N>` line.

2. **Cramming multiple actions into one shot.** One dominant action per shot — this is a hard H3 constraint. If the brief describes sequential actions, split them across multiple shots with cuts.

3. **Forgetting camera motion.** Every shot needs an explicit camera movement specification — even "Static Shot" if the camera doesn't move. Omitting it leaves the model guessing.

4. **Inconsistent character identity across shots.** Repeat identity anchors (hair, clothing, key props) in every shot, phrased freshly but consistently. If hair gets messy in shot 3, it stays messy in shot 4.

5. **Wrong shot count for duration.** Respect the budget: 4–6s → 1–2 shots; 7–10s → 2–3 shots; 11–15s → 3–5 shots. Don't plan 5 shots for a 5-second video.

6. **Mixing diegetic and non-diegetic music.** Music audible to characters (radio, live performance, phone speaker) goes in the shot description. Background score the characters can't hear goes in `non_diegetic_music`. Never put the same music in both.

7. **Inventing reference labels (Ref2VA).** Never create labels beyond those defined in `subject_definitions`. `<Subject 3>` means the same thing in every section where it appears. Different indices for different modalities are numbered independently.

8. **Translating or rewriting dialogue.** Preserve the user's exact words inside `<d>` tags — including punctuation, hesitations, and language. Never translate to English if the user wrote in another language.

9. **Skipping the style opener.** The `detailed_description` (Ref2VA) or `integrated_multimodal_description` (Base) MUST open with 1–2 sentences of overall style BEFORE `[Shot 1]`. Don't jump straight into the first shot.

10. **Flat timestamps.** `[Shot 1]` never has a timestamp. Every subsequent shot needs `At MM:SS.mmm` with strictly increasing times. Convert duration to seconds with proper formatting (8s → 8.00 for the instruction line).

## Verification Checklist

- [ ] Mode correctly detected from attached assets and user intent
- [ ] All seven enhancement dimensions applied (camera, look, pacing, character, spatial, continuity, sound)
- [ ] Output contains ONLY the required H3 fields — no preamble, no markdown fences, no extra commentary
- [ ] Timestamps are strictly increasing and fall within duration_s
- [ ] Exactly ONE dominant action per shot
- [ ] Camera motion specified for every shot (type + amplitude + speed)
- [ ] Character identity consistent across all shots (repeat anchors every shot)
- [ ] Dialogue preserved verbatim in `<d>[Language] ...</d>` format
- [ ] Style opener present before `[Shot 1]`
- [ ] Reference labels (Ref2VA only) used consistently and never invented beyond subject_definitions
- [ ] Duration (4–15s) and aspect ratio specified or assumed-and-flagged
- [ ] No named IP, celebrities, or trademarked character names
- [ ] Diegetic music in shot descriptions, non-diegetic score in `non_diegetic_music`