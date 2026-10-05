# Calliope 1.5.9 — deterministic H3 prompts, agent scratch workspace, vision speed

Release **v1.5.9**, 2026-10-05. Spans the **previous release (1.5.8) → now**: it includes merged **PR #83** (contributor @happymy, `dev10` → `main`, merged as `94853b9`) **plus** the working-tree changes on top (51 files modified, 2 added, 2 deleted; +966 / −1,940 lines).
Tests: **716 passed, 1 skipped**; `svelte-check` **0 errors**; `contracts.json` regenerated.

---

## Included: PR #83 — by @happymy (merged `94853b9`)

*"llama.cpp router 模式适配、防工作流内置 input 污染生成物、视频生成 Prompt 生成时机优化和批量生成功能等。"* — 53 files, ~+5,124/−247, 17 commits. Four pillars:

1. **LLM Profile system + llama.cpp router-mode adaptation.** New `llm_probe.py`: model discovery and endpoint test endpoints, per-profile thinking control via `chat_template_kwargs`, and request shaping that works against llama.cpp-router style endpoints (new `agent/llm.py` plumbing, ~+332). Settings → LLM gains profile management; the per-role agent assignments ride the same profiles.
2. **Context-window-aware history budget.** The agent history budget is now derived from the model's context window instead of a fixed 400k-char default, and an oversized-history situation raises `ContextWindowTooSmall` instead of silently truncating mid-exchange (`tests/test_llm_context_budget.py`).
3. **Strict Workflow mode — ON by default.** New `workflows.strict_mode` column (migration backfills existing workflows to 1 on next startup): when strict, ComfyUI nodes whose values Calliope didn't explicitly set are pruned/reset instead of running with whatever defaults were baked into the workflow JSON — baked-in prompts/refs can no longer pollute renders ("防工作流内置 input 污染生成物"). Toggle per workflow in Settings → Workflows. (`tests/test_strict_mode.py`)
4. **Video prompt generation timing + batch pre-compilation.** `enqueue_video_jobs` restructured into two passes — compile every clip's H3 prompt first, then write job rows — so the LLM held the GPU alone and ComfyUI only started once the batch's prompts existed, with a dead-endpoint circuit breaker (one timeout per batch, not per clip). New endpoint `POST /api/jobs/projects/{id}/batch-prompt` ("Compile prompts" button) fills drafts up front. **Retuned in this update** (section 1 below): the compile pass is now deterministic-first and `batch-prompt` defaults to `force=false` — the VRAM dance is gone entirely because enqueue no longer calls the LLM at all.

Also in the PR: story-router chunking tests, LLM JSON-mode tests (`test_llm_json.py`), shell-tool and security hardening tests, workspace-plugin stats for the planner.

---

## ⚠ Breaking changes (uncommitted work on top of PR #83)

- **Continue-from-previous (video extend) system removed.** The Script-stage **Chain/Unchain clips** toggle and the Video-stage **Video source** picker (Auto / Upload / From timeline) are gone, along with the backend `chain_from_prev` API fields, the enqueue auto-chain resolution, the `continue_source` job payload, and `worker._resolve_continue_source`. **A reference video is now purely a workflow concern**: an `(Input:video)` node appears as a normal form tile on the Video stage and you supply the file yourself (upload, or pick a rendered clip / Build-Scene capture from the asset picker). Existing projects with chain flags set simply ignore them (the column survives as dead legacy data).
- **Prompts never reference videos anymore.** The MiniMax H3 prompt format no longer emits `<Video N> is the motion reference …` lines. If you relied on that wording, hand-write directives in the form's Text Prompt instead — your text is now sent **verbatim**.
- **`POST /api/jobs/projects/{id}/batch-prompt` default changed**: `force` now defaults to `false` (instant deterministic compile). Pass `force: true` explicitly for the LLM rewrite pass.
- **H3 preview resolution order changed** (details below). Saved drafts and the `prompt_draft_meta.based_on` hash remain compatible; video files no longer participate in the draft-freshness signature (swapping a reference video no longer stales a draft).

---

## 1 · H3 prompt pipeline: deterministic-first, user text wins

The Project → Video → Generate flow no longer waits on a local LLM to resolve a prompt.

- **Default preview/enqueue is instant.** The six-section H3 format (`subject_definitions` → … → `non_diegetic_music`) is compiled directly from the scene beat + subject roster — zero LLM calls on the default path (no continuity-ledger refresh, no rewrite, no critic).
- **Your typed prompt is never replaced.** A prompt typed on the clip form (the composer's prompt-role textarea) flows verbatim through preview → modal → job. Previously the form text was silently ignored — the backend substituted its own compile (the "`<video 1>~ to extend video continue`" report).
- **LLM rewrite is opt-in**: only the modal's **Regenerate** (`force: true`) refreshes the continuity ledger, runs the rewrite (120 s real wall-clock cap via `asyncio.wait_for`, dead-endpoint breaker), and runs the nine-check critic. The critic never runs on the instant paths.
- **`<Video N>` grounding deleted** everywhere: rewrite system rules, rosters, fallback template, vision parts (videos are workflow wiring, not prompt content), and the draft hash.
- **Batch enqueue never calls the LLM** (Pass 1 is now fully deterministic); the single-GPU VRAM concern disappears because there is no LLM in the enqueue path at all.
- New helper `stored_continuity_plan` (`continuity.py`) reads the persisted plan without refreshing.
- New regression tests: default path must not call the LLM; typed prompt wins verbatim (incl. the `<video 1>` report case); enqueue must not call the LLM.
- Docs rewritten: AGENTS.md prompt-profile + review-gate sections; README "Prompt profiles", "Video inputs are workflow-owned", "Review the prompt before you generate".

Key files: `agent/video_agent.py` (major rewrite), `agent/prompts.py`, `agent/continuity.py`, `models/schemas.py`, `tests/test_scene_settings.py`, `tests/test_continuity.py`, `tests/test_batch_prompt.py`, `tests/test_h3_profile.py`.

## 2 · Chain/continue removal (frontend)

- Deleted `ClipSourceModal.svelte` (−303 lines) and its QueueStage wiring: clip-source state, hidden video file input, timeline-clip options, workflow-has-video-input gating, `queue.chainDisabledReason` / `clipSource.*` / `script.chainOn|Off|Title` / `videoEdit.videoSource|noVideoInputWf|continueHint` — 14 i18n keys × 7 locales.
- `ScriptStage.svelte`: Chain/Unchain toggle removed.
- `comfy/types.ts`: `chain_from_prev` and `clip_source` dropped from the `Scene`/`Clip`/`SceneVideoSettings` types; scenes API responses stop surfacing the column.
- `tests/test_video_continue.py` deleted (−572 lines); `test_clips.py` updated.

## 3 · Agent scratch workspace (file tools)

The Canvas agent — and **every swarm sub-agent role** — can now draft text files at runtime, then commit approved content into the project with the normal scene/clip/beat tools.

- New plugin `agent/harness/plugins/files.py`: `list_files` / `read_file` / `write_file` (overwrite|append) / `delete_file`, scoped to one folder per session — `workspace_dir/sessions/<session_id>` — shared by the main loop and its sub-agents.
- Containment guard (`_files_guard`, pre-execute): relative paths only, no `..`, the shell's sensitive-name deny-list (`.env`, `calliope.db`, config, keys…), realpath containment (symlink-safe) → new machine-readable code **`guard_workspace_path`**. Caps: read ≤100k chars, write ≤200k, list ≤200 entries; UTF-8 text only; directories refused by delete.
- Tools added to all four `ROLE_TOOLS` roles; the new `files` prompt section (order 32, "SCRATCH WORKSPACE") renders the session folder + the draft → show → commit workflow into main-loop AND sub-agent system prompts. Hidden on Build Scene (shot-only scope), section returns None there.
- `contracts.json` regenerated (+4 tool names + guard code). New `tests/test_workspace_files.py` (19 tests: roundtrip, containment matrix, symlink escape, session isolation, sandbox visibility, scene-origin hiding, role coverage, contracts).
- Docs: AGENTS.md "Scratch file tools" section; README "Agent workspace" paragraph; Settings → System Paths hint updated in all 7 locales.

## 4 · Vision input speed (4K fix)

User-attached images and H3 reference images no longer crawl on cloud APIs.

- **Dimension gate**: an image passes through unresized only if ≤512 KB **and** long edge ≤1280 px. Previously the gate was bytes-only, so a 4K image that compressed small (WebP / hard-squeezed JPEG) hit the API at full 3840 px — vision APIs tile by pixels (~8–20× the tokens). The re-encode ladder now scales by the **long edge** (1024→768→640), so portrait 4K can't blow its height past the cap.
- **Data-URL cache**: bounded LRU (~32) keyed `(path, mtime_ns, size)`. History is re-derived before every step of an agent turn and the H3 vision-grounded Regenerate re-attaches the same references — each call previously re-read + re-decoded + re-encoded the full-res file; now it's once per file until edited.
- Tests: true-4K-under-512KB must resize to ≤1024 px; cache hit + mtime invalidation. (`tests/test_agent_event_log.py`)

## 5 · Sub-agent / planner reasoning streaming (UI feedback)

- `_run_sub_agent` runs the streaming `chat_stream` and publishes `agent.thinking` per reasoning delta (agent_name-tagged); `_plan` streams as `planner`. The chat's working card no longer sits as a bare "Agent is working…" pill for minutes on local models.
- `ReasoningPanel.svelte` auto-opens while a stream is live (manual toggle still stands); Canvas + Build Scene pages cap the accumulated stream to the last 24k chars so a whole swarm's thinking can't grow unbounded.
- Tests: `tests/test_sub_agent_guards.py::test_sub_agent_streams_reasoning_events` (+ prior-report digest updates).

## 6 · Canvas chat "Agent" button + H3 vision setting (Settings)

- Canvas collapsed chat now shows a labeled **✦ Agent** pill (was a bare chevron) with a positioning-anchor fix (the button no longer lands on top of the language dropdown), larger hit area, and focus-visible outlines. i18n `canvas.agentButton` ×7.
- New setting **`h3_rewrite_vision`** (default **OFF**): attaches reference images/frames to the H3 rewrite as vision parts. Off, the rewrite grounds on asset text descriptions (fast); on, the model sees the actual files — warned to be minutes-slow on local llama.cpp mmproj. Exposed in Settings → Queue with label/hint ×7 locales; config + settings-router + `Settings` type wired. (`test_h3_rewrite_extra_body.py` covers the request extras path.)

---

## Files of note

| Change | Files |
|---|---|
| Deleted | `calliope-web/src/lib/components/video/ClipSourceModal.svelte`, `calliope-backend/tests/test_video_continue.py` |
| Added | `calliope-backend/src/calliope/agent/harness/plugins/files.py`, `calliope-backend/tests/test_workspace_files.py`, `docs/changelogs/2026-10-05.md` |
| Largest rewrites | `agent/video_agent.py` (−388 net), `VideoEditWorkspace.svelte` (−163), `QueueStage.svelte` (−116), `orchestrator.py` (+101) |

## Upgrade notes

1. **Restart the backend** — the deterministic preview and all speed fixes require loading this code; a stale process still runs the old LLM-bound path.
2. Projects that used chain/continue: re-pick the extend workflow per clip and supply the reference video through the Video-stage form tile (or leave it empty and let ComfyUI error).
3. `minimax_h3_ref` users who saved drafts: drafts stay valid; a reference-**video** swap no longer marks them stale (by design — videos don't influence the prompt text).
4. Settings → Queue gains an "H3 vision" checkbox (off by default) — leave it off unless your rewrite endpoint handles vision quickly.
