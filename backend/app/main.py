from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .action_api import router as action_router
from .browser_agent_api import router as browser_agent_router
from .browser_agent_monitoring_api import router as browser_agent_monitoring_router
from .browser_agent_registry_api import router as browser_agent_registry_router
from .catalog_api import router as catalog_router
from .commerce.api import router as commerce_router
from .dashboard_api import router as dashboard_router
from .db import engine
from .fixed_procurement_source_api import router as fixed_procurement_source_router
from .inventory_api import router as inventory_router
from .kaspi_order_polling import LAST_RUN as KASPI_POLL_STATUS
from .kaspi_order_polling import polling_loop
from .marketplace_api import router as marketplace_router
from .marketplace_orders_api import router as marketplace_orders_router
from .monitoring_api import router as monitoring_router
from .monitoring_center_api import router as monitoring_center_router
from .pricing_api import router as pricing_router
from .product_commerce_api import router as product_commerce_router
from .product_detail_api import router as product_detail_router
from .product_economics_api import router as product_economics_router
from .product_identity_api import router as product_identity_router
from .product_registry_api import router as product_registry_router
from .product_supplier_binding_api import router as product_supplier_binding_router
from .product_xml_import_api import router as product_xml_import_router
from .products import router as products_router
from .purchase_api import router as purchase_router
from .supplier_products_api import router as supplier_products_router
from .supplier_state_api import router as supplier_state_router
from .suppliers import router as suppliers_router
from .ui import router as ui_router

APP_VERSION = "0.18.0"
DEPLOYMENT_MARKER = "kaspi-archive-enrichment-and-polling-v1"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="LEO CRM API",
    version=APP_VERSION,
    description="Backend for product monitoring, pricing, XML, orders and purchases.",
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(ui_router)
app.include_router(products_router)
app.include_router(product_detail_router)
app.include_router(product_economics_router)
app.include_router(inventory_router)
app.include_router(fixed_procurement_source_router)
app.include_router(product_commerce_router)
app.include_router(action_router)
app.include_router(product_registry_router)
app.include_router(product_supplier_binding_router)
app.include_router(product_xml_import_router)
app.include_router(catalog_router)
app.include_router(suppliers_router)
app.include_router(supplier_products_router)
app.include_router(supplier_state_router)
app.include_router(dashboard_router)
app.include_router(monitoring_router)
app.include_router(monitoring_center_router)
app.include_router(browser_agent_router)
app.include_router(browser_agent_monitoring_router)
app.include_router(browser_agent_registry_router)
app.include_router(pricing_router)
app.include_router(marketplace_router)
app.include_router(marketplace_orders_router)
app.include_router(commerce_router)
app.include_router(product_identity_router)
app.include_router(purchase_router)


@app.on_event("startup")
async def start_kaspi_order_polling() -> None:
    stop_event = asyncio.Event()
    app.state.kaspi_poll_stop_event = stop_event
    app.state.kaspi_poll_task = asyncio.create_task(polling_loop(stop_event))


@app.on_event("shutdown")
async def stop_kaspi_order_polling() -> None:
    stop_event = getattr(app.state, "kaspi_poll_stop_event", None)
    task = getattr(app.state, "kaspi_poll_task", None)
    if stop_event is not None:
        stop_event.set()
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@app.get("/")
async def root() -> dict[str, object]:
    return {
        "service": "leo-crm",
        "status": "running",
        "version": APP_VERSION,
        "deployment_marker": DEPLOYMENT_MARKER,
        "docs": "/docs",
        "crm": "/crm",
        "kaspi_polling": dict(KASPI_POLL_STATUS),
    }


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "database": "not_checked",
        "version": APP_VERSION,
        "deployment_marker": DEPLOYMENT_MARKER,
        "timestamp": datetime.now(UTC).isoformat(),
        "kaspi_polling": dict(KASPI_POLL_STATUS),
    }


@app.get("/ready")
async def ready():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "database": "unavailable",
                "version": APP_VERSION,
                "deployment_marker": DEPLOYMENT_MARKER,
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )
    return {
        "status": "ready",
        "database": "ok",
        "version": APP_VERSION,
        "deployment_marker": DEPLOYMENT_MARKER,
        "timestamp": datetime.now(UTC).isoformat(),
    }
