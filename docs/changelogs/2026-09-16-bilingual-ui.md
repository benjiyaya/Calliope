# Calliope 1.5.2 — bilingual UI (English / 中文) and one-command local start

Every user-facing string used to be hardcoded English, spread across ~60
components — so a Chinese-speaking user saw an English UI no matter how the app
was configured, and there was no single place to change a label. This release
routes the whole interface through a dictionary layer with a language switcher
in the header.

## What changed

- **Full en/zh dictionary** — 1001 keys, mirrored 1:1 between `en.ts` and
  `zh.ts`. Both are typed against `Dict` (`typeof en`), so the compiler rejects
  a translation that is missing a key.
- **Every component wired through `t()`** — nav rail, canvas, projects, story,
  assets, script, queue, the video workspace, Build Scene, settings, agent chat,
  toasts, and the error paths. No hardcoded English copy remains in the UI.
- **Language switcher in the header** — English / 中文. The choice is stored in
  `localStorage` (`calliope-lang`) and applied on reload; a missing or
  unrecognized value falls back to English.
- **Placeholder interpolation** — `t('key', { name: … })` substitutes `{name}`,
  with a `count` shorthand that also fills `{n}`, the convention most
  count-bearing strings use.
- **Genre / tone / duration options** moved into `formOptions.ts`, mapping the
  English values the backend stores onto dictionary keys — so stored project
  data keeps working regardless of the display language.
- **Local start/stop scripts** — `setup.bat` (idempotent first-time install),
  `start.bat` (two visible windows), `start-bg.bat` (background, output to
  `logs/`), and `stop.bat`.

## Upgrade

No migration and no new dependencies. `git pull`, then reload the frontend — an
`npm install` in `calliope-web/` is only needed if you are coming from an older
release. The UI defaults to English; switch to 中文 with the dropdown in the
header.
