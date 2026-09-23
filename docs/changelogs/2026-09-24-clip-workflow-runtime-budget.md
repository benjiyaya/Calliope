# Calliope 1.5.7 — per-clip workflows, runtime budgets, media-path guards

Three reported issues fixed — [#66](https://github.com/benjiyaya/Calliope/issues/66)
(picking a workflow for one clip changed every clip in the scene),
[#64](https://github.com/benjiyaya/Calliope/issues/64) (scripts ignored the
story's target length), and
[#67](https://github.com/benjiyaya/Calliope/issues/67) (a directory path
passed as a video input crashed ComfyUI with `Errno 21`).

## #66 — Workflow choice is per clip

The Video stage's workflow picker was keyed by scene id, so choosing a
workflow for one clip silently overwrote every sibling clip of the same
scene — a scene set up as image→video on clip 1 + video→video extend on
clip 2 could never be enqueued as designed.

- The picker is now keyed by **clip id**, and opening a clip hydrates its
  own persisted choice (`clips.workflow_id`, then `form_workflow_id`).
- The debounced autosave persists both `clips.workflow_id` and
  `form_workflow_id`, so the choice survives restarts.
- **Generate One** and batch **Generate All** each resolve the workflow per
  clip: session pick → clip row → saved form choice → scene default → first
  enabled. A mixed-workflow scene now renders each clip with its own
  workflow.

## #64 — Target runtime is a budget, not a floor

Asking for a 30-second film could produce a 1:51 script: prompts carried no
per-scene scale, and the duration rescale could only grow
(`max(target/total, 1.0)`), never shrink.

- Script prompts (single-call and chunked) now state the **total seconds**
  and a concrete **per-scene budget** (`Per-scene budget: ~Ns each — write
  action length and dialogue VOLUME to this scale`), so content volume is
  written to the runtime instead of discovered after.
- The final rescale may now go **below 1.0** (floor 0.5): an over-written
  board's scene budgets shrink toward the target. Content is never edited —
  long scenes remain correct; clips are where they get cut.

## #67 — Media inputs that are directories fail fast

When the agent hand-wrote a path into a media input slot it could pass
ComfyUI's input *directory*; that value reached ComfyUI and died with the
opaque `ValueError: [Errno 21] Is a directory`.

- `prepare_media_inputs` now rejects a media input whose local-looking value
  is a directory or nonexistent file **before queueing**, failing with
  `node N (class): field value '...' is a directory` / `file not found
  locally` — with the node id and actionable guidance. Bare names (no path
  separators) are legitimate Comfy-side references and pass untouched.
- The agent's `run_workflow` mirrors the guard at **call time**: an
  `input_values[<media node>]` value that resolves to a dir/missing file
  fails the tool call with correction guidance instead of creating a doomed
  job.

## Testing

- `tests/test_script_chunking.py` — per-scene budget present in both prompt
  builders; oversized-script durations shrink toward the target.
- `tests/test_video_continue.py` — directory and missing-file rejection,
  bare-name passthrough, existing-file upload still works.
- Full backend suite: 534 passed; `svelte-check`: 0 errors.
