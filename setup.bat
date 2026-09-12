@echo off
chcp 936 >nul
setlocal
set ROOT=%~dp0
set BACKEND=%ROOT%calliope-backend
set WEB=%ROOT%calliope-web

echo ============================================
echo  Calliope First-time setup / Calliope 首次安装（幂等，可重复运行）
echo ============================================

if not exist "%BACKEND%\.venv\Scripts\python.exe" (
    echo [1/2] Creating venv and installing backend deps / 创建虚拟环境并安装后端依赖...
    cd /d "%BACKEND%"
    python -m venv .venv
    call .venv\Scripts\activate.bat
    .venv\Scripts\python -m pip install --upgrade pip
    .venv\Scripts\pip install -e ".[dev]"
) else (
    echo [1/2] Backend deps already installed - skipping / 后端依赖已安装，跳过。
)

if not exist "%WEB%\node_modules" (
    echo [2/2] Installing frontend deps, running npm install... / 安装前端依赖 npm install...
    cd /d "%WEB%"
    call npm install
) else (
    echo [2/2] Frontend deps already installed - skipping / 前端依赖已安装，跳过。
)

echo.
echo Setup complete / 安装完成。Copy calliope_config.example.json to calliope_config.json before first launch to configure LLM/ComfyUI, or configure in the in-app Settings page / 首次启动前可复制 calliope_config.example.json 为 calliope_config.json 配置 LLM/ComfyUI，也可以在应用内 Settings 页面配置。
echo.
pause
