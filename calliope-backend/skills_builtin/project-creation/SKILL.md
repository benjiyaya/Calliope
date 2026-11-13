---
name: project-creation
description: "Use when a story idea should become a project through the script. Stop at the script. The user generates the view."
version: 1.0.0
license: MIT
metadata:
  tags: [story, script, project]
---

# Project creation

Turn a story idea into a project the user can read. Stop when the script is on the board.

## Steps

1. If this chat is not linked to a project, call `create_project` with the title and the idea. That links the chat and opens the project canvas. If the chat is already linked, do not create another project.
2. Read the idea. Draft the story bible: characters, locations, items, and beats. Use `add_character`, `add_location`, `add_item`, `add_beat`, or `generate_story`. `get_beat` before you write a scene from a beat.
3. Write the screenplay from those beats with `generate_script` or `add_scene`. Keep each scene's heading, action, and spoken lines.
4. Stop.

## Stop

Do not call `enqueue_asset_jobs`. Do not call `enqueue_video_jobs`. Do not call `break_into_shots` unless the user asked for shots.

Image sheets, location references, and video clips are the user's next step. Tell them the script is ready and that generation waits until they ask for it.
