$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$ApiUrl = "https://leo-crm-api.onrender.com"
$AgentId = "leo-windows-$env:COMPUTERNAME"
$ProfileDir = Join-Path $RepoRoot ".browser-agent-profile"

function Find-Chrome {
    $candidates = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
        "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }
    throw "Chrome или Edge не найден. Установите Google Chrome и повторите запуск."
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python не найден в PATH. Установите Python 3.12 и включите Add Python to PATH."
}

if (-not (Test-Path ".venv")) {
    Write-Host "Создаю виртуальное окружение..." -ForegroundColor Cyan
    python -m venv .venv
}

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
Write-Host "Проверяю зависимости..." -ForegroundColor Cyan
& $Python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зависимости." }

$SecureToken = Read-Host "Вставьте SERVICE_API_TOKEN из Render" -AsSecureString
$TokenPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureToken)
try {
    $Token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($TokenPtr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($TokenPtr)
}
if ([string]::IsNullOrWhiteSpace($Token)) { throw "Токен не введён." }

$Chrome = Find-Chrome
New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null

$ChromeArgs = @(
    "--remote-debugging-port=9222",
    "--user-data-dir=$ProfileDir",
    "--no-first-run",
    "--no-default-browser-check",
    "https://www.ozon.ru/"
)

$ExistingCdp = $false
try {
    Invoke-RestMethod -Uri "http://127.0.0.1:9222/json/version" -TimeoutSec 2 | Out-Null
    $ExistingCdp = $true
} catch {}

if (-not $ExistingCdp) {
    Write-Host "Запускаю браузер Browser Agent..." -ForegroundColor Cyan
    Start-Process -FilePath $Chrome -ArgumentList $ChromeArgs
    Start-Sleep -Seconds 4
}

try {
    Invoke-RestMethod -Uri "http://127.0.0.1:9222/json/version" -TimeoutSec 5 | Out-Null
} catch {
    throw "Chrome CDP не отвечает на порту 9222. Закройте Chrome и запустите файл ещё раз."
}

$env:CRM_API_URL = $ApiUrl
$env:CRM_SERVICE_TOKEN = $Token
$env:BROWSER_AGENT_ID = $AgentId
$env:CHROME_CDP_ENDPOINT = "http://127.0.0.1:9222"
$env:BROWSER_AGENT_POLL_SECONDS = "3"
$env:BROWSER_AGENT_CONCURRENCY = "1"
$env:BROWSER_AGENT_DISPATCH_LIMIT = "100"

Write-Host ""
Write-Host "Browser Agent запущен." -ForegroundColor Green
Write-Host "CRM: $ApiUrl"
Write-Host "Agent: $AgentId"
Write-Host "Не закрывайте это окно и окно Chrome. Для остановки нажмите Ctrl+C."
Write-Host ""

& $Python -m tools.browser_agent
exit $LASTEXITCODE
