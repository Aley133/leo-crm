@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_browser_agent.ps1"
set EXIT_CODE=%ERRORLEVEL%
echo.
pause
exit /b %EXIT_CODE%
