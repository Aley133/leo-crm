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
