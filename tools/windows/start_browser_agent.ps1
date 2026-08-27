param([switch]$Once)

$ErrorActionPreference = "Stop"
$ConfigPath = Join-Path $PSScriptRoot "browser_agent.env.ps1"
$RuntimeRoot = Join-Path $env:LOCALAPPDATA "LEO-CRM\browser-agent"
$SourceRoot = Join-Path $RuntimeRoot "source"
$VenvRoot = Join-Path $RuntimeRoot ".venv"
$PythonExe = Join-Path $VenvRoot "Scripts\python.exe"
$RepositoryZip = "https://github.com/Aley133/leo-crm/archive/refs/heads/main.zip"

if (-not (Test-Path $ConfigPath)) { throw "Missing configuration: $ConfigPath" }
. $ConfigPath
if ([string]::IsNullOrWhiteSpace($env:CRM_API_URL)) { throw "CRM_API_URL is required" }
if ([string]::IsNullOrWhiteSpace($env:CRM_SERVICE_TOKEN) -or $env:CRM_SERVICE_TOKEN -like "PASTE_*") { throw "A real CRM_SERVICE_TOKEN is required" }
if ([string]::IsNullOrWhiteSpace($env:BROWSER_AGENT_ID)) { $env:BROWSER_AGENT_ID = "leo-http-$env:COMPUTERNAME" }
if ([string]::IsNullOrWhiteSpace($env:BROWSER_AGENT_CONCURRENCY)) { $env:BROWSER_AGENT_CONCURRENCY = "3" }
if ([string]::IsNullOrWhiteSpace($env:BROWSER_AGENT_POLL_SECONDS)) { $env:BROWSER_AGENT_POLL_SECONDS = "3" }

New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
$TempZip = Join-Path $env:TEMP "leo-crm-main.zip"
$TempExtract = Join-Path $env:TEMP "leo-crm-http-agent-extract"
if (Test-Path $TempZip) { Remove-Item $TempZip -Force }
if (Test-Path $TempExtract) { Remove-Item $TempExtract -Recurse -Force }
Invoke-WebRequest -Uri $RepositoryZip -OutFile $TempZip -UseBasicParsing
Expand-Archive -Path $TempZip -DestinationPath $TempExtract -Force
$ExtractedRoot = Join-Path $TempExtract "leo-crm-main"
if (Test-Path $SourceRoot) { Remove-Item $SourceRoot -Recurse -Force }
Move-Item -Path $ExtractedRoot -Destination $SourceRoot
Remove-Item $TempZip -Force
Remove-Item $TempExtract -Recurse -Force

if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw "Install Python 3.12 or newer" }
if (-not (Test-Path $PythonExe)) { & py -3 -m venv $VenvRoot }
& $PythonExe -m pip install --disable-pip-version-check -q -r (Join-Path $SourceRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Unable to install HTTP agent dependencies" }

$Health = Invoke-RestMethod -Uri ($env:CRM_API_URL.TrimEnd('/') + "/health") -TimeoutSec 20
Write-Host "CRM version: $($Health.version); marker: $($Health.deployment_marker)" -ForegroundColor Green
Write-Host "Starting HTTP monitoring; Chrome/Playwright are not used. WB is temporarily disabled." -ForegroundColor Green
Set-Location $SourceRoot
$PythonArgs = @("-m", "tools.browser_agent")
if ($Once) { $PythonArgs += "--once" }
& $PythonExe @PythonArgs
exit $LASTEXITCODE
