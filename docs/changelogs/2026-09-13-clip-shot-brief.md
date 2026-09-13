# Calliope 1.5.1 — per-clip shot brief in the Video stage

A patch for the 1.5 scene→clips model. 1.5 moved rendering to clips
(`#1.1`, `#1.2`, …), but the Video stage still only showed the *scene's*
script. The composer's prompt field gave no hint of what an individual clip
was supposed to perform, so writing the workflow prompt meant copying text
out of the Script tab by hand.

## What changed

- **Shot brief panel** at the top of the Video composer: the selected clip's
  `#N.M` label, shot size, duration, and its description — the exact text the
  clip's video prompt is built from. A **Copy** button puts it on the
  clipboard for the prompt field.
- Expanding the brief also shows the **dialog lines that shot performs**
  (derived from `dialog_lines_covered`, the same 1-based indexing the backend
  uses) and the scene's characters.
- **Legacy un-expanded clips** fall back to the scene's action text and say
  so, pointing at **Break into shots** in the Script stage.
- Video stage is a **two-column layout**: player + filmstrip + scene script on
  the left, all generation inputs in a scrollable right-hand inspector, so the
  input stack can no longer squeeze the video player out of view.
- Removed the duplicated **Heading** block from the scene Script drawer — the
  heading already shows in the row above it.
- The filmstrip thumbnails' hover title now includes the clip description.

## Upgrade

No migration, no new dependencies. `git pull`, then `npm install` in
`calliope-web/` is not required unless you were on an older release — pull and
reload the frontend.
