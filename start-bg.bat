@echo off
chcp 936 >nul
setlocal
set ROOT=%~dp0
set BACKEND=%ROOT%calliope-backend
set WEB=%ROOT%calliope-web

if not exist "%BACKEND%\.venv\Scripts\python.exe" (
    echo [错误] 后端未安装，请先运行 setup.bat
    pause
    exit /b 1
)
if not exist "%WEB%\node_modules" (
    echo [错误] 前端未安装，请先运行 setup.bat
    pause
    exit /b 1
)

if not exist "%ROOT%logs" mkdir "%ROOT%logs"

echo 后台启动后端...  日志: logs\backend.log
powershell -NoProfile -Command "$p = Start-Process -FilePath '%ROOT%calliope-backend\.venv\Scripts\python.exe' -ArgumentList '-m','calliope.main','--host','127.0.0.1','--port','8247' -WorkingDirectory '%ROOT%calliope-backend' -RedirectStandardOutput '%ROOT%logs\backend.log' -RedirectStandardError '%ROOT%logs\backend.err.log' -PassThru -WindowStyle Hidden; Write-Host ('Backend PID -> ' + $p.Id)"

echo 后台启动前端...  日志: logs\web.log
powershell -NoProfile -Command "$p = Start-Process -FilePath 'npm.cmd' -ArgumentList 'run','dev' -WorkingDirectory '%ROOT%calliope-web' -RedirectStandardOutput '%ROOT%logs\web.log' -RedirectStandardError '%ROOT%logs\web.err.log' -PassThru -WindowStyle Hidden; Write-Host ('Web npm PID -> ' + $p.Id)"

echo.
echo 后台服务已启动：后端 http://127.0.0.1:8247  前端 http://127.0.0.1:5173
echo 用 stop.bat 停止。