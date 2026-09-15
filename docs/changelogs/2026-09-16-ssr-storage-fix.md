# Calliope 1.5.3 — fix the "localStorage.getItem is not a function" crash on Node 22+

1.5.2 could fail to render at all. The page came up behind a Vite error overlay
reading `TypeError: localStorage.getItem is not a function`, with the stack
pointing at `initialLanguage()` in `lib/i18n.svelte.ts`.

A patch rather than a feature: the language layer was correct, but its storage
read was not safe to run on the server.

## What changed

- **The guard was wrong, not just incomplete.** The code sampled the global's
  existence:

  ```ts
  if (typeof localStorage === 'undefined') return 'en';
  const stored = localStorage.getItem(STORAGE_KEY);
  ```

  That is safe in a browser, but Node 22+ ships a web-storage global, and when
  it is enabled without a usable `--localstorage-file` the binding is a plain
  object with **no Storage methods**. The `typeof` check therefore passes and
  the next line throws. Presence is not the same as usability.
- **New `lib/storage.ts`** probes for a callable method instead of trusting the
  binding to exist, and falls back to `null` for every shape the server can
  present — absent, method-less, and throwing-on-access (sandboxed frames,
  blocked storage). Write failures (private mode, quota) are swallowed.
- **Only module- and component-scope reads were affected.** SvelteKit evaluates
  those on the server too, which is why `i18n.svelte.ts` broke the whole app.
  The same guard inside `onMount` / `$effect` never runs on the server and was
  harmless — worth knowing, since the idiom appears throughout the codebase.
- **A second latent crash fixed.** The canvas page's `railCollapsed` /
  `chatCollapsed` `$state` initializers had the identical flaw and would have
  failed as soon as the first one was fixed.
- **Regression guard** — `npm test` runs `scripts/check-storage-ssr.mjs`, which
  exercises the helper against all four storage shapes and fails the build if
  the unsafe `typeof localStorage` idiom reappears anywhere in `src/`.

Verified by reverting to the previous version: `/build-scene` returned
`HTTP 500 Internal Error` before the fix and `HTTP 200` with full server-rendered
output after it. `npm run check` and `npm run build` both pass.

## Upgrade

No migration and no new dependencies. `git pull` and reload. If you were seeing
the error overlay, it will be gone — no need to change your Node version.
