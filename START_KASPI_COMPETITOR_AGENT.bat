@echo off
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\start_kaspi_competitor_agent_windows.ps1"
if errorlevel 1 (
  echo.
  echo Kaspi Competitor Agent stopped with an error.
  pause
)
endlocal
