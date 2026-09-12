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

echo 打开两个窗口：
echo   后端 http://127.0.0.1:8247
echo   前端 http://127.0.0.1:5173
echo 关闭窗口即停止对应服务。

start "Calliope Backend" /D "%BACKEND%" cmd /k ".venv\Scripts\python -m calliope.main --host 127.0.0.1 --port 8247"
start "Calliope Web" /D "%WEB%" cmd /k "npm run dev"