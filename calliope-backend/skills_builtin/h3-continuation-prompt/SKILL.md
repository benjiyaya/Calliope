---
name: h3-continuation-prompt
description: "Use when writing the prompt for a clip that continues the previous clip via H3 motion context"
version: 1.0.0
license: MIT
metadata:
  tags: [video, prompt-engineering, minimax-h3, motion-context, continuity]
  related_skills: [h3-video-prompt-enhancer]
---

# H3 Continuation Prompt

## Overview

A clip rendered with an H3 motion-context workflow does not start from
nothing: the previous clip's saved latent is pinned at its head, so the
picture and the sound are already moving when the new clip begins. The
prompt has to agree with that. A prompt written as a fresh shot fights the
pinned frames and produces a visible restart at the join.

In Calliope a clip continues the clip before it in playback order unless
"Start a new chain here" is ticked on it. Film export joins a continued pair
with a hard cut, not a crossfade.

Write the clip's prompt in the usual H3 six-section format
(`subject_definitions` → `summary` → `retention_analysis` →
`detailed_description` → `overall_soundscape` → `non_diegetic_music`) and
apply the rules below. Load `h3-video-prompt-enhancer` for the format itself.

## Rules

1. **Same place, same environment, same camera.** Copy the place, the light, and the camera height, angle, and distance from the previous clip into `summary` and the opening of `detailed_description`. A new angle is a cut, and only when this clip's action leaves the location. Load `video-continuation`.
2. **Same subjects, same labels.** Keep `subject_definitions` and the
   reference labels (`<Subject N>`, `<Picture N>`) identical across the whole
   chain. Renumbering or rewording a subject reads to the model as a new
   subject.
3. **Open on continuation only.** `detailed_description` begins already in
   progress, mid-action: same camera position and distance, same pace, same
   light, the same surfaces passing. No new scenery nouns in the opening.
   For the first ~2 s nothing new enters the frame and the subject does not
   stop, turn or reach.
4. **Shift timestamps by the pinned head.** The first 22 frames are pinned
   context and get trimmed from the delivered clip. Write every timestamp
   0.92 s later than where it should land in the final cut (an event meant
   at 1.0 s in the film is written at 1.92 s).
5. **No progressive camera move across a join.** Never carry a push-in,
   orbit or similar progressive move over a join; finish it inside one clip.
   Put joins mid-walk or mid-action, never at a doorway or threshold.
6. **Music continues.** `non_diegetic_music`: "continuing the same … at the
   same tempo, without a new entry or a build", unless a change is the intent.
7. **Dialogue pacing.** About one short line per 2.5 s. Split dialogue
   between sentences, never mid-sentence across a join.
8. **Leave something to continue.** End each clip with the action still in
   progress, so the next clip has motion to pick up.

## Checklist

- [ ] Place, light, and camera height/angle/distance copied from the previous clip
- [ ] `subject_definitions` and reference labels copied verbatim from the previous clip
- [ ] Opening sentence is mid-action, no new nouns, no stop/turn/reach in the first ~2 s
- [ ] Every timestamp +0.92 s
- [ ] No push-in/orbit spanning the join; the join is not at a threshold
- [ ] Music line says "continuing the same … at the same tempo"
- [ ] Dialogue ≤ one short line per 2.5 s, split between sentences
- [ ] The clip ends mid-action
