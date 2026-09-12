@echo off
chcp 936 >nul
setlocal
set ROOT=%~dp0

echo 正在停止 Calliope 后端和前端进程...

powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(python|node)(\.exe)?$' -and $_.CommandLine -match 'calliope' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host ('Killed ' + $_.Name + ' PID ' + $_.ProcessId) }"

echo.
echo 已停止。若还有残留窗口，可直接关闭对应 cmd 窗口。
echo.
pause