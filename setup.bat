@echo off
chcp 936 >nul
setlocal
set ROOT=%~dp0
set BACKEND=%ROOT%calliope-backend
set WEB=%ROOT%calliope-web

echo ============================================
echo  Calliope 首次安装（幂等，可重复运行）
echo ============================================

if not exist "%BACKEND%\.venv\Scripts\python.exe" (
    echo [1/2] 创建虚拟环境并安装后端依赖...
    cd /d "%BACKEND%"
    python -m venv .venv
    call .venv\Scripts\activate.bat
    .venv\Scripts\python -m pip install --upgrade pip
    .venv\Scripts\pip install -e ".[dev]"
) else (
    echo [1/2] 后端依赖已安装，跳过。
)

if not exist "%WEB%\node_modules" (
    echo [2/2] 安装前端依赖 npm install...
    cd /d "%WEB%"
    call npm install
) else (
    echo [2/2] 前端依赖已安装，跳过。
)

echo.
echo 安装完成。首次启动前可复制 calliope_config.example.json 为 calliope_config.json 配置 LLM/ComfyUI，
echo 也可以在应用内 Settings 页面配置。
echo.
pause