param(
    [switch]$Once
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$ConfigPath = Join-Path $PSScriptRoot "browser_agent.env.ps1"
$ChromeProfile = Join-Path $RepoRoot ".browser-agent\chrome-profile"
$CdpEndpoint = "http://127.0.0.1:9222"

if (-not (Test-Path $ConfigPath)) {
    Write-Host "Missing configuration: $ConfigPath" -ForegroundColor Red
    Write-Host "Copy browser_agent.env.example.ps1 to browser_agent.env.ps1 and fill CRM_SERVICE_TOKEN." -ForegroundColor Yellow
    exit 2
}

. $ConfigPath

if ([string]::IsNullOrWhiteSpace($env:CRM_API_URL)) {
    throw "CRM_API_URL is required in browser_agent.env.ps1"
}
if ([string]::IsNullOrWhiteSpace($env:CRM_SERVICE_TOKEN)) {
    throw "CRM_SERVICE_TOKEN is required in browser_agent.env.ps1"
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

$ChromeCandidates = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$ChromeExe = $ChromeCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $ChromeExe) {
    throw "Google Chrome was not found. Install Chrome or update start_browser_agent.ps1."
}

New-Item -ItemType Directory -Force -Path $ChromeProfile | Out-Null

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

Set-Location $RepoRoot
$PythonArgs = @("-m", "tools.browser_agent")
if ($Once) {
    $PythonArgs += "--once"
    Write-Host "Running one-job verification mode..." -ForegroundColor Yellow
}
else {
    Write-Host "Starting continuous browser monitoring..." -ForegroundColor Green
}

& py @PythonArgs
exit $LASTEXITCODE
