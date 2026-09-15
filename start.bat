@echo off
setlocal
set ROOT=%~dp0
set BACKEND=%ROOT%calliope-backend
set WEB=%ROOT%calliope-web

if not exist "%BACKEND%\.venv\Scripts\python.exe" (
    echo [Error] Backend not installed - run setup.bat first
    pause
    exit /b 1
)
if not exist "%WEB%\node_modules" (
    echo [Error] Frontend not installed - run setup.bat first
    pause
    exit /b 1
)

echo Opening two windows
echo   Backend:  http://127.0.0.1:8247
echo   Frontend: http://127.0.0.1:5173
echo Close a window to stop that service

start "Calliope Backend" /D "%BACKEND%" cmd /k ".venv\Scripts\python -m calliope.main --host 127.0.0.1 --port 8247"
start "Calliope Web" /D "%WEB%" cmd /k "npm run dev"
