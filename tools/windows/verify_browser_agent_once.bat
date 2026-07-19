@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_browser_agent.ps1" -Once
set EXIT_CODE=%ERRORLEVEL%
echo.
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
