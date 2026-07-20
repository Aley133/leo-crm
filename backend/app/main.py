from datetime import UTC, datetime

from fastapi import FastAPI
from sqlalchemy import text

from .browser_agent_api import router as browser_agent_router
from .browser_agent_monitoring_api import router as browser_agent_monitoring_router
from .db import engine
from .marketplace_api import router as marketplace_router
from .marketplace_orders_api import router as marketplace_orders_router
from .monitoring_api import router as monitoring_router
from .pricing_api import router as pricing_router
from .product_identity_api import router as product_identity_router
from .products import router as products_router
from .purchase_api import router as purchase_router
from .supplier_products_api import router as supplier_products_router
from .supplier_state_api import router as supplier_state_router
from .suppliers import router as suppliers_router

APP_VERSION = "0.13.0"
DEPLOYMENT_MARKER = "supplier-state-control-plane-v1"

app = FastAPI(
    title="LEO CRM API",
    version=APP_VERSION,
    description="Backend for product monitoring, pricing, XML, orders and purchases.",
)

app.include_router(products_router)
app.include_router(suppliers_router)
app.include_router(supplier_products_router)
app.include_router(monitoring_router)
app.include_router(supplier_state_router)
app.include_router(browser_agent_router)
app.include_router(browser_agent_monitoring_router)
app.include_router(pricing_router)
app.include_router(marketplace_router)
app.include_router(marketplace_orders_router)
app.include_router(product_identity_router)
app.include_router(purchase_router)


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "service": "leo-crm",
        "status": "running",
        "version": APP_VERSION,
        "deployment_marker": DEPLOYMENT_MARKER,
        "docs": "/docs",
    }


@app.get("/health")
async def health() -> dict[str, str]:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))

    return {
        "status": "ok",
        "database": "connected",
        "version": APP_VERSION,
        "deployment_marker": DEPLOYMENT_MARKER,
        "timestamp": datetime.now(UTC).isoformat(),
    }
