# Calliope — Electron shell

Portable Windows packaging for the Calliope story-to-video studio.
Double-click `Calliope.exe` → the FastAPI backend starts → the window opens the UI.
No installer, no code signing, no vite — the backend serves the built SPA at its
own origin, so there is zero CORS/API-base configuration.

## Prerequisites

- `calliope-backend/.venv` set up (`pip install -e ".[dev]"` + `pyinstaller`)
- `calliope-web/node_modules` installed
- `electron/node_modules` installed (`npm install` in this folder — once)

## Build (one command)

```bash
cd electron
npm run build:all
```

This chains:

1. `build:web` — `npm run build` in `calliope-web/` (fresh SPA in `calliope-web/build/`)
2. `build:backend` — PyInstaller (`calliope-backend/calliope.spec`) →
   `calliope-backend/dist/calliope-backend/` (onedir, SPA bundled as `calliope/static`)
3. `dist` — electron-builder `dir` target → `electron/dist/win-unpacked/`

Then zip for distribution:

```bash
npm run zip        # → electron/dist/Calliope-win-x64.zip
```

## Run

- Packaged: `electron/dist/win-unpacked/Calliope.exe` (or unzip the zip anywhere
  writable and run `win-unpacked/Calliope.exe`). First launch shows a splash while
  the backend boots; a failure page appears if it cannot start.
- Dev (shell against the source backend in `calliope-backend/.venv`):
  `npm start` in this folder.

Ports: tries 8247, then 8248→8250. If a healthy Calliope backend already answers
on a candidate port (e.g. a dev instance), it is reused instead of spawning a
second one. Set `CALLIOPE_PORT=<n>` to pin the preferred port.

> Note: the backend reads host/port from env (`CALLIOPE_HOST`/`CALLIOPE_PORT`),
> not argv — there is no CLI parser in `calliope.main`. The shell sets both.
> Also: if `ELECTRON_RUN_AS_NODE=1` is set in your environment, Electron will not
> start as an app — unset it before launching.

## Where user data lives

Everything is **portable**, next to the backend exe — never `%TEMP%`, never the
source tree:

```
<app>/resources/backend/calliope-backend.exe
<app>/resources/backend/data/                 created on first run (fresh, empty)
<app>/resources/backend/data/calliope.db      SQLite database
<app>/resources/backend/data/assets/          generated assets / uploads
<app>/resources/backend/calliope_config.json  created only when you save Settings
```

This works because `calliope-backend/src/calliope/config.py` anchors
`BACKEND_ROOT` to `sys.executable`'s folder when running frozen
(`getattr(sys, "frozen", False)`); non-frozen dev behavior is unchanged.
The package ships with **no** `data/` and **no** `calliope_config.json` —
fresh users get a fresh database and sane in-memory defaults.

Move/copy the whole unzipped folder to relocate the app + its data together.
Avoid `Program Files` (not writable without elevation).

## Updating backend / frontend

- UI changed → `npm run build:web` then rebuild (`build:backend` + `dist`).
  The spec packs `calliope-web/build/` directly; the copy in
  `calliope-backend/src/calliope/static/` is only used by the dev server.
- Backend Python changed → `npm run build:backend && npm run dist`.
- To ship an update, re-zip. Users replace the app folder; their `data/` and
  `calliope_config.json` survive if they keep those two paths (they sit outside
  `_internal/`, directly in `resources/backend/`).

## Implementation notes

- `main.js`: single-instance lock; spawns `resources/backend/calliope-backend.exe`
  (hidden, `windowsHide`) with `CALLIOPE_HOST`/`CALLIOPE_PORT`; polls
  `/api/health` up to 30 s; loads `http://127.0.0.1:<port>/`; kills the backend on
  `before-quit` / `window-all-closed` / `exit` / `SIGINT` / `SIGTERM`.
- Backend exe console stays ON (`console=True` in `calliope.spec`) so users can
  run it from a terminal for diagnostics; the shell always hides it.
- asar stays enabled: only the JS shell is packed (`resources/app.asar`); the
  backend lives in `extraResources` → `resources/backend/` and writes beside
  itself, so asar is irrelevant to storage.

## Known rough edges

- Default Electron icon (no custom `.ico` yet).
- Full-page reload (F5) on a deep route (`/project/123`) returns JSON 404 —
  Starlette `StaticFiles(html=True)` has no SPA fallback. In-app navigation is
  client-side and unaffected; the window always starts at `/`, which works.
- Force-killing `Calliope.exe` (Task Manager → End task with /F) can orphan
  `calliope-backend.exe`; the next launch simply reuses it if it is healthy.
- Windows SmartScreen will warn on first run (unsigned binary).
