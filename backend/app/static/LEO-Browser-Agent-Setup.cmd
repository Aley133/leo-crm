@echo off
setlocal
title LEO CRM Browser Agent Setup
color 0B
echo.
echo ==============================================
echo       LEO CRM Browser Agent Setup
echo ==============================================
echo.
echo Downloading the latest Browser Agent...

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $root=Join-Path $env:LOCALAPPDATA 'LEO-CRM\browser-agent-bootstrap'; $zip=Join-Path $env:TEMP 'leo-crm-main.zip'; $extract=Join-Path $env:TEMP 'leo-crm-browser-agent-download'; if(Test-Path $zip){Remove-Item $zip -Force}; if(Test-Path $extract){Remove-Item $extract -Recurse -Force}; New-Item -ItemType Directory -Force -Path $root | Out-Null; Invoke-WebRequest -Uri 'https://github.com/Aley133/leo-crm/archive/refs/heads/main.zip' -OutFile $zip -UseBasicParsing; Expand-Archive -Path $zip -DestinationPath $extract -Force; $source=Join-Path $extract 'leo-crm-main'; if(-not (Test-Path (Join-Path $source 'tools\start_browser_agent_windows.ps1'))){throw 'Browser Agent launcher was not found in the downloaded package.'}; if(Test-Path (Join-Path $root 'source')){Remove-Item (Join-Path $root 'source') -Recurse -Force}; Move-Item $source (Join-Path $root 'source'); Remove-Item $zip -Force; Remove-Item $extract -Recurse -Force; & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'source\tools\start_browser_agent_windows.ps1'); exit $LASTEXITCODE"

if errorlevel 1 (
  echo.
  echo Browser Agent setup stopped with an error.
  echo Copy the error text and send it to the project chat.
  pause
  exit /b 1
)

endlocal
