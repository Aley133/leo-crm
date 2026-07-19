param(
    [switch]$Once
)

$ErrorActionPreference = "Stop"
$ConfigPath = Join-Path $PSScriptRoot "browser_agent.env.ps1"
$RuntimeRoot = Join-Path $env:LOCALAPPDATA "LEO-CRM\browser-agent"
$SourceRoot = Join-Path $RuntimeRoot "source"
$ChromeProfile = Join-Path $RuntimeRoot "chrome-profile"
$VenvRoot = Join-Path $RuntimeRoot ".venv"
$PythonExe = Join-Path $VenvRoot "Scripts\python.exe"
$CdpEndpoint = "http://127.0.0.1:9222"
$RepositoryZip = "https://github.com/Aley133/leo-crm/archive/refs/heads/main.zip"

if (-not (Test-Path $ConfigPath)) {
    Write-Host "Missing configuration: $ConfigPath" -ForegroundColor Red
    Write-Host "Keep browser_agent.env.ps1 in the same folder as this launcher." -ForegroundColor Yellow
    exit 2
}

. $ConfigPath

if ([string]::IsNullOrWhiteSpace($env:CRM_API_URL)) {
    throw "CRM_API_URL is required in browser_agent.env.ps1"
}
if ([string]::IsNullOrWhiteSpace($env:CRM_SERVICE_TOKEN) -or $env:CRM_SERVICE_TOKEN -like "PASTE_*") {
    throw "A real CRM_SERVICE_TOKEN is required in browser_agent.env.ps1"
}

$env:CHROME_CDP_ENDPOINT = $CdpEndpoint
if ([string]::IsNullOrWhiteSpace($env:BROWSER_AGENT_ID)) {
    $env:BROWSER_AGENT_ID = "leo-$env:COMPUTERNAME"
}
if ([string]::IsNullOrWhiteSpace($env:BROWSER_AGENT_CONCURRENCY)) {
    $env:BROWSER_AGENT_CONCURRENCY = "3"
}
if ([string]::IsNullOrWhiteSpace($env:BROWSER_AGENT_POLL_SECONDS)) {
    $env:BROWSER_AGENT_POLL_SECONDS = "3"
}

New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
New-Item -ItemType Directory -Force -Path $ChromeProfile | Out-Null

Write-Host "Preparing current LEO CRM browser-agent source..." -ForegroundColor Cyan
$TempZip = Join-Path $env:TEMP "leo-crm-main.zip"
$TempExtract = Join-Path $env:TEMP "leo-crm-browser-agent-extract"
if (Test-Path $TempZip) { Remove-Item $TempZip -Force }
if (Test-Path $TempExtract) { Remove-Item $TempExtract -Recurse -Force }
Invoke-WebRequest -Uri $RepositoryZip -OutFile $TempZip -UseBasicParsing
Expand-Archive -Path $TempZip -DestinationPath $TempExtract -Force
$ExtractedRoot = Join-Path $TempExtract "leo-crm-main"
if (-not (Test-Path (Join-Path $ExtractedRoot "tools\browser_agent.py"))) {
    throw "Downloaded repository does not contain tools\browser_agent.py"
}
if (Test-Path $SourceRoot) { Remove-Item $SourceRoot -Recurse -Force }
Move-Item -Path $ExtractedRoot -Destination $SourceRoot
Remove-Item $TempZip -Force
Remove-Item $TempExtract -Recurse -Force

$PyLauncher = Get-Command py -ErrorAction SilentlyContinue
if (-not $PyLauncher) {
    throw "Python launcher 'py' was not found. Install Python 3.12 or newer from python.org."
}

if (-not (Test-Path $PythonExe)) {
    Write-Host "Creating isolated Python environment..." -ForegroundColor Cyan
    & py -3 -m venv $VenvRoot
    if ($LASTEXITCODE -ne 0) { throw "Unable to create Python virtual environment" }
}

Write-Host "Installing browser-agent dependencies..." -ForegroundColor Cyan
& $PythonExe -m pip install --disable-pip-version-check -q -r (Join-Path $SourceRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Unable to install Python dependencies" }

$ChromeCandidates = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$ChromeExe = $ChromeCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $ChromeExe) {
    throw "Google Chrome was not found. Install Chrome and run this launcher again."
}

try {
    Invoke-RestMethod -Uri "$CdpEndpoint/json/version" -TimeoutSec 2 | Out-Null
    Write-Host "Chrome CDP already available on port 9222." -ForegroundColor Green
}
catch {
    Write-Host "Starting dedicated Chrome profile for LEO CRM..." -ForegroundColor Cyan
    Start-Process -FilePath $ChromeExe -ArgumentList @(
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=9222",
        "--user-data-dir=$ChromeProfile",
        "--no-first-run",
        "--no-default-browser-check",
        "https://www.ozon.ru/"
    )

    $Ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        try {
            Invoke-RestMethod -Uri "$CdpEndpoint/json/version" -TimeoutSec 2 | Out-Null
            $Ready = $true
            break
        }
        catch {}
    }
    if (-not $Ready) {
        throw "Chrome started, but CDP endpoint did not become available on port 9222."
    }
}

Write-Host "Checking CRM..." -ForegroundColor Cyan
$Health = Invoke-RestMethod -Uri ($env:CRM_API_URL.TrimEnd('/') + "/health") -TimeoutSec 20
Write-Host "CRM version: $($Health.version); marker: $($Health.deployment_marker)" -ForegroundColor Green

Set-Location $SourceRoot
$PythonArgs = @("-m", "tools.browser_agent")
if ($Once) {
    $PythonArgs += "--once"
    Write-Host "Running one-job verification mode..." -ForegroundColor Yellow
}
else {
    Write-Host "Starting continuous browser monitoring..." -ForegroundColor Green
}

& $PythonExe @PythonArgs
exit $LASTEXITCODE
