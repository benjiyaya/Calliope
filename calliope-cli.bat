@echo off
chcp 65001 >nul
setlocal
set "ROOT=%~dp0"
set "BACKEND=%ROOT%calliope-backend"
set "PY=%BACKEND%\.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [错误] 后端未安装 - 请先运行 setup.bat
    exit /b 1
)

"%PY%" -m calliope.cli.main %*
exit /b %ERRORLEVEL%