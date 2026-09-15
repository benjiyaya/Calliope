@echo off
setlocal
set ROOT=%~dp0

echo Stopping Calliope backend and frontend processes...

powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(python|node)(\.exe)?$' -and $_.CommandLine -match 'calliope' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host ('Killed ' + $_.Name + ' PID ' + $_.ProcessId) }"

echo.
echo Done. If leftover windows remain, close them manually.
echo.
pause
