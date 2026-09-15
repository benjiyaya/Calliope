# Calliope 1.5.2 — seven-language UI (en/zh/es/fr/de/ja/ko) and local start scripts

Every user-facing string used to be hardcoded English, spread across dozens of
components — so a non-English user saw an English UI no matter how the app was
configured, and there was no single place to change a label. The interface now
resolves through a dictionary layer, with a language switcher in the header.

## What changed

- **Seven languages** — English, 中文, Español, Français, Deutsch, 日本語,
  한국어. Each dictionary carries the full 1001 keys and is typed against `Dict`
  (`typeof en`), so a translation that is missing a key fails the build rather
  than silently rendering the key.
- **Language switcher in the header** — the choice is stored in `localStorage`
  (`calliope-lang`) and applied on reload; a missing or unrecognized stored
  value falls back to English instead of blanking the UI.
- **Every component wired through `t()`** — nav rail, canvas, projects, story,
  assets, script, queue, the video workspace, Build Scene, settings, agent chat,
  toasts, and the error paths. No hardcoded English copy remains in the UI.
- **Placeholder interpolation** — `t('key', { name: … })` substitutes `{name}`,
  with a `count` shorthand that also fills `{n}`, the convention most
  count-bearing strings use.
- **Genre / tone / duration options** moved into `formOptions.ts`, mapping the
  English values the backend stores onto dictionary keys — so stored project
  data keeps working regardless of the display language.
- **Backend version reports correctly** — `/api/health` and the OpenAPI schema
  read `__version__`, which had been stuck at `1.4.1` while 1.5.0 and 1.5.1
  shipped. It now matches the frontend at `1.5.2`.
- **Local start/stop scripts** — `setup.bat` (idempotent first-time install),
  `start.bat` (two visible windows), `start-bg.bat` (background, output to
  `logs/`), and `stop.bat`, all with English output so non-Chinese locales
  render them without mojibake.

## Upgrade

No migration and no new dependencies. `git pull`, then reload the frontend. The
UI defaults to English; pick another language from the dropdown in the header.
