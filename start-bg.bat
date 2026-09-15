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

if not exist "%ROOT%logs" mkdir "%ROOT%logs"

echo Starting backend in background...   Log: logs\backend.log
powershell -NoProfile -Command "$p = Start-Process -FilePath '%ROOT%calliope-backend\.venv\Scripts\python.exe' -ArgumentList '-m','calliope.main','--host','127.0.0.1','--port','8247' -WorkingDirectory '%ROOT%calliope-backend' -RedirectStandardOutput '%ROOT%logs\backend.log' -RedirectStandardError '%ROOT%logs\backend.err.log' -PassThru -WindowStyle Hidden; Write-Host ('Backend PID -> ' + $p.Id)"

echo Starting frontend in background...   Log: logs\web.log
powershell -NoProfile -Command "$p = Start-Process -FilePath 'npm.cmd' -ArgumentList 'run','dev' -WorkingDirectory '%ROOT%calliope-web' -RedirectStandardOutput '%ROOT%logs\web.log' -RedirectStandardError '%ROOT%logs\web.err.log' -PassThru -WindowStyle Hidden; Write-Host ('Web npm PID -> ' + $p.Id)"

echo.
echo Background services started:
echo   Backend:  http://127.0.0.1:8247
echo   Frontend: http://127.0.0.1:5173
echo Run stop.bat to stop them.
