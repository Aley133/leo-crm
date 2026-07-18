from datetime import UTC, datetime

from fastapi import FastAPI
from sqlalchemy import text

from .db import engine
from .monitoring_api import router as monitoring_router
from .products import router as products_router
from .supplier_products_api import router as supplier_products_router
from .suppliers import router as suppliers_router

app = FastAPI(
    title="LEO CRM API",
    version="0.6.1",
    description="Backend for product monitoring, pricing, XML, orders and purchases.",
)

app.include_router(products_router)
app.include_router(suppliers_router)
app.include_router(supplier_products_router)
app.include_router(monitoring_router)


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "service": "leo-crm",
        "status": "running",
        "docs": "/docs",
    }


@app.get("/health")
async def health() -> dict[str, str]:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))

    return {
        "status": "ok",
        "database": "connected",
        "timestamp": datetime.now(UTC).isoformat(),
    }
