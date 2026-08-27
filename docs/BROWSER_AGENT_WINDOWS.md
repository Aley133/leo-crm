# Windows HTTP Monitoring Agent

The local agent keeps the existing CRM monitoring queue and lease protocol but executes Ozon checks through the lab-proved HTTP session. It does not launch Chrome or Playwright. Wildberries targets remain stored and are temporarily excluded from dispatch.

## Setup

1. Install the current `LEO-Browser-Agent-Setup.exe` release.
2. Enter `SERVICE_API_TOKEN` on first launch.
3. If no compatible lab session is found, copy an Ozon search request from DevTools Network as **Copy as cURL (bash)** and paste it into the prompt once.

The Ozon profile is stored under the current Windows user and encrypted with DPAPI. Cookies and request headers are never uploaded to CRM; only normalized price, availability, seller and delivery facts are returned.

The agent uses three bounded HTTP workers by default. Existing CRM monitor targets, observations, price calculations and Fast Dumping triggers are unchanged.

## Session renewal

When Ozon expires the session, relaunch the agent and import a fresh `/search/` cURL. The old session is replaced locally. Do not share the session file or cURL text.
