---
name: video-continuation
description: "Use when the next MiniMax H3 clip must keep the same place, environment, and camera angle as the clip before it."
version: 1.0.0
license: MIT
metadata:
  tags: [video, minimax-h3, continuity, camera]
  related_skills: [h3-video-prompt-enhancer, h3-continuation-prompt]
---

# Video continuation

A generated clip is about ten seconds. The next clip is a new generation. If the story is still in the same place, the prompt has to carry the place, the environment, and the camera, or the angle jumps.

Load `h3-video-prompt-enhancer` for the six-section format. Apply the rules below inside those sections.

## What to copy

From the previous clip, copy these and do not paraphrase them into something new:

- **Place.** The room, street, valley, or set. Name the same walls, floor, horizon, and landmarks.
- **Environment.** Time of day, weather, light direction, color of the light, haze, and the materials of the surfaces.
- **Camera.** Height (eye level, low, high), angle (front, three-quarter, profile, overhead), distance (wide, medium, close), lens feel, and whether the camera is locked or moving. If it is locked, say it is locked. If it is moving, finish that move inside this clip.

`subject_definitions` keeps the same subject names and the same `<Picture N>` labels.

## Where it goes

`summary` states the place and the camera in one sentence.

`detailed_description` opens on that same camera. The first second does not cut, reframe, or reveal a new room. Name the place and the light again in the first sentence, then the action. A push-in, pan, or orbit that is still in progress at the end of the previous clip does not continue across the join; start this clip on the camera the story is holding.

`retention_analysis` says which picture is the place reference and which is the character, so the environment is not left to the action line alone.

## When the story does change

Change the camera or the place only when this clip's action leaves the location or the script calls a new angle. Write the new angle as a cut at the start of this clip, and describe the new place with the same amount of detail: surfaces, light, landmarks, height, angle, and distance.
