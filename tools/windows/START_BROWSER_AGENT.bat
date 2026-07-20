@echo off
setlocal
cd /d "%~dp0\..\.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0\..\start_browser_agent_windows.ps1"
if errorlevel 1 (
  echo.
  echo Browser Agent stopped with an error.
  pause
)
endlocal
