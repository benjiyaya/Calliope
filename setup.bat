@echo off
setlocal
set ROOT=%~dp0
set BACKEND=%ROOT%calliope-backend
set WEB=%ROOT%calliope-web

echo ============================================
echo  Calliope first-time setup - idempotent, safe to re-run
echo ============================================

if not exist "%BACKEND%\.venv\Scripts\python.exe" (
    echo [1/2] Creating venv and installing backend deps...
    cd /d "%BACKEND%"
    python -m venv .venv
    call .venv\Scripts\activate.bat
    .venv\Scripts\python -m pip install --upgrade pip
    .venv\Scripts\pip install -e ".[dev]"
) else (
    echo [1/2] Backend deps already installed - skipping
)

if not exist "%WEB%\node_modules" (
    echo [2/2] Installing frontend deps, running npm install...
    cd /d "%WEB%"
    call npm install
) else (
    echo [2/2] Frontend deps already installed - skipping
)

echo.
echo Setup complete.
echo Before the first launch, copy calliope_config.example.json to calliope_config.json
echo to configure LLM/ComfyUI, or configure them on the in-app Settings page.
echo.
pause
