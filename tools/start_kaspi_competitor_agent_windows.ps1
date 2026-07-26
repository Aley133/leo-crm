$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$ApiUrl = "https://leo-crm-api.onrender.com"
$AgentId = "kaspi-competitor-$env:COMPUTERNAME"

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

$env:CRM_API_URL = $ApiUrl
$env:CRM_SERVICE_TOKEN = $Token
$env:KASPI_COMPETITOR_AGENT_ID = $AgentId
$env:KASPI_COMPETITOR_POLL_SECONDS = "3"
$env:KASPI_COMPETITOR_CONCURRENCY = "2"

Write-Host ""
Write-Host "LEO Kaspi Competitor Agent запущен." -ForegroundColor Green
Write-Host "CRM: $ApiUrl"
Write-Host "Agent: $AgentId"
Write-Host "Используется обычный HTTP-клиент из проверенного архива."
Write-Host "Browser Agent поставщиков и Chrome не затрагиваются."
Write-Host "Для остановки нажмите Ctrl+C."
Write-Host ""

& $Python -m tools.kaspi_competitor_agent
exit $LASTEXITCODE
