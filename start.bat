@echo off
chcp 936 >nul
setlocal
set ROOT=%~dp0
set BACKEND=%ROOT%calliope-backend
set WEB=%ROOT%calliope-web

if not exist "%BACKEND%\.venv\Scripts\python.exe" (
    echo [Error/错误] Backend not installed - run setup.bat first / 后端未安装，请先运行 setup.bat
    pause
    exit /b 1
)
if not exist "%WEB%\node_modules" (
    echo [Error/错误] Frontend not installed - run setup.bat first / 前端未安装，请先运行 setup.bat
    pause
    exit /b 1
)

echo Opening two windows / 打开两个窗口：
echo   Backend/后端  http://127.0.0.1:8247
echo   Frontend/前端  http://127.0.0.1:5173
echo Close a window to stop that service / 关闭窗口即停止对应服务

start "Calliope Backend" /D "%BACKEND%" cmd /k ".venv\Scripts\python -m calliope.main --host 127.0.0.1 --port 8247"
start "Calliope Web" /D "%WEB%" cmd /k "npm run dev"
