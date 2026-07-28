# LEO CRM

Personal automation platform for Kaspi marketplace operations.

## Current foundation

- FastAPI backend
- Render deployment configuration
- Health-check endpoint
- API documentation through Swagger UI

## Local start

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn backend.app.main:app --reload
```

Open:

- `http://127.0.0.1:8000/`
- `http://127.0.0.1:8000/health`
- `http://127.0.0.1:8000/docs`

## Architecture direction

The database will be the source of truth. XML, Telegram and the web interface will be clients or generated outputs of the core platform.

## Telegram procurement price alerts

Set both deployment variables to deliver sudden supplier price-drop events:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

The alert detector uses the monitoring history and emits when an available
supplier price falls by at least 50% from its recent median baseline. Alerts
are disabled by default and must be enabled per product in its CRM card. The
same card includes a temporary test-notification action for verifying the
Telegram deployment settings.
