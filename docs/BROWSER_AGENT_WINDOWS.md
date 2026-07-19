# LEO CRM Browser Agent — Windows

The local Browser Agent is the execution surface for supplier pages that block Render IPs. It connects only to a dedicated Chrome profile on `127.0.0.1:9222`; the Chrome debugging port is never exposed to the internet.

## First verification

1. Install Google Chrome and Python 3.12+.
2. Clone/pull the repository and install dependencies:

   ```powershell
   py -m pip install -r requirements.txt
   ```

3. Copy:

   `tools/windows/browser_agent.env.example.ps1`

   to:

   `tools/windows/browser_agent.env.ps1`

4. Put the same service token used in Swagger into `CRM_SERVICE_TOKEN`.
5. In Swagger, queue one known Ozon target:

   `POST /api/monitor-targets/{target_id}/queue-browser-agent`

6. Double-click:

   `tools/windows/verify_browser_agent_once.bat`

The launcher will:

- start a dedicated persistent Chrome profile;
- bind CDP only to `127.0.0.1:9222`;
- verify the CRM health endpoint;
- claim exactly one job;
- open the supplier page through the live Chrome profile;
- return price/currency/availability to CRM;
- exit.

A successful terminal result prints a payload containing at least `price`, `currency`, `observed_at`, and `adapter_schema_version`.

## Continuous mode

After the one-job verification succeeds, double-click:

`tools/windows/start_browser_agent_continuous.bat`

The continuous agent runs one dispatcher plus `BROWSER_AGENT_CONCURRENCY` parallel workers. Each `MonitorTarget` retains its own `next_check_at`; this is not a full-catalog sequential scan.

## Security rules

- Never forward port 9222 on the router.
- Never bind Chrome debugging to `0.0.0.0`.
- Never commit `browser_agent.env.ps1`.
- The dedicated profile is stored under `.browser-agent/chrome-profile` and is excluded from Git.
- Do not use the main personal Chrome profile for the agent.
