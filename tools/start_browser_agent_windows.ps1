$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
$ApiUrl = "https://leo-crm-api.onrender.com"
$AgentId = "leo-http-$env:COMPUTERNAME"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python не найден в PATH. Установите Python 3.12 и включите Add Python to PATH."
}
if (-not (Test-Path ".venv")) {
    Write-Host "Создаю виртуальное окружение..." -ForegroundColor Cyan
    python -m venv .venv
}
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
& $Python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зависимости." }

$SecureToken = Read-Host "Вставьте SERVICE_API_TOKEN из Render" -AsSecureString
$TokenPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureToken)
try { $Token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($TokenPtr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($TokenPtr) }
if ([string]::IsNullOrWhiteSpace($Token)) { throw "Токен не введён." }

$env:CRM_API_URL = $ApiUrl
$env:CRM_SERVICE_TOKEN = $Token
$env:BROWSER_AGENT_ID = $AgentId
$env:BROWSER_AGENT_POLL_SECONDS = "3"
$env:BROWSER_AGENT_CONCURRENCY = "3"
$env:BROWSER_AGENT_DISPATCH_LIMIT = "100"

Write-Host "HTTP Monitoring Agent запущен: Chrome и Playwright не требуются." -ForegroundColor Green
Write-Host "Ozon использует локальную зашифрованную HTTP-сессию; WB временно отключён."
& $Python -m tools.browser_agent
exit $LASTEXITCODE
