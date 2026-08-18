# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Calliope FastAPI backend (one-folder mode).
#
# Build (from calliope-backend/):
#   ./.venv/Scripts/python.exe -m PyInstaller calliope.spec --noconfirm
#
# Output:
#   dist/calliope-backend/calliope-backend.exe  (+ _internal/ next to it)
#
# Storage note: when frozen, config.py anchors calliope_config.json and data/
# NEXT TO the exe (portable app), so keep this folder user-writable.

import os

from PyInstaller.utils.hooks import collect_submodules

# SPECPATH is the directory containing this spec file (calliope-backend/).
BACKEND_DIR = os.path.abspath(SPECPATH)

# Fresh SvelteKit adapter-static build. Bundled as calliope/static so the frozen
# exe serves the SPA at its own origin (main.py mounts <package>/static at /).
WEB_BUILD = os.path.abspath(os.path.join(BACKEND_DIR, "..", "calliope-web", "build"))

# main.py imports uvicorn dynamically via __import__("uvicorn"), which static
# analysis cannot see — collect all uvicorn submodules (lifespan/loops/protocols)
# plus its optional speedups and other lazily-imported packages explicitly.
hidden = collect_submodules("uvicorn")
hidden += ["httptools", "websockets", "multipart", "dotenv"]


a = Analysis(
    # launcher.py imports calliope.main as a package module — see its docstring
    # for why the entry must NOT be src/calliope/main.py itself.
    [os.path.join(BACKEND_DIR, "launcher.py")],
    pathex=[BACKEND_DIR, os.path.join(BACKEND_DIR, "src")],
    binaries=[],
    datas=[(WEB_BUILD, "calliope/static")],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "pytest_asyncio", "ruff"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="calliope-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # faster builds, fewer antivirus false-positives
    # Console stays ON so a user can run calliope-backend.exe from a terminal and
    # read uvicorn/ComfyUI error logs when debugging. The Electron shell always
    # spawns it with windowsHide=True, so no console window appears in normal use.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="calliope-backend",
)
