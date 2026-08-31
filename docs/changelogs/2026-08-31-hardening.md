# Calliope 1.3.2 — prompt preview hardening

Follow-up to 1.3.0's prompt review gate. Fixes the failure mode where an
unreachable or slow LLM endpoint left the **Review prompt** modal hanging on
"Resolving prompt…" until the connection died (`Failed to fetch`), and made
Generate impossible.

## Fixed

- **Preview requests no longer double-fire.** The modal's resolve effect was
  subscribed to its own request store and re-fired on every state transition
  while the prompt text was still empty — producing duplicate (sometimes
  parallel) `POST /preview-prompt` calls per scene. The effect now fires
  exactly once per scene.
- **Dead LLM endpoints fail fast.** The preview rewrite now times out after
  **30 s** (enqueue keeps the 120 s headroom for thinking models) and returns
  the deterministic six-section H3 template instead of hanging.
- **The modal is never a dead end.** When the rewrite can't be resolved, the
  editor is populated with the raw scene text (heading + action + dialog) and
  a hint explains the situation. You can edit that text and Generate — the
  confirmed text wins — or hit **Regenerate** to retry. Previously an error
  left an empty editor with a spinner-less dead end and re-firing requests
  on every reopen.

## Notes

- The new client-side fallback text is **not** H3-formatted; that's
  intentional and labeled in the UI. Generate sends it verbatim via the
  confirmed-prompt override.
- If you hit this in the wild, also check **Settings → LLM**: a wrong base
  URL or API key is the usual cause of ~60 s hangs (a proxy cutting an idle
  connection to a dead endpoint).

## Upgrade

```bash
git pull
cd calliope-web && npm install
```

Backend deps unchanged. Restart both processes (`start.bat`).
