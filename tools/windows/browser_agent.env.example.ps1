# Copy this file to browser_agent.env.ps1 in the same folder.
# browser_agent.env.ps1 is local-only and must never be committed.

$env:CRM_API_URL = "https://leo-crm-api.onrender.com"
$env:CRM_SERVICE_TOKEN = "PASTE_THE_SAME_SERVICE_TOKEN_USED_IN_SWAGGER"

$env:BROWSER_AGENT_ID = "leo-home-pc"
$env:BROWSER_AGENT_CONCURRENCY = "3"
$env:BROWSER_AGENT_POLL_SECONDS = "3"
$env:BROWSER_AGENT_DISPATCH_LIMIT = "100"
