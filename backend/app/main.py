from datetime import UTC, datetime

from fastapi import FastAPI

app = FastAPI(
    title="LEO CRM API",
    version="0.1.0",
    description="Backend for product monitoring, pricing, XML, orders and purchases.",
)


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "service": "leo-crm",
        "status": "running",
        "docs": "/docs",
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "timestamp": datetime.now(UTC).isoformat(),
    }
