from __future__ import annotations

import asyncio
import contextlib
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
from .db import SessionLocal, engine
from .kaspi_order_dispatch import dispatch_stale_kaspi_orders
from .kaspi_seller.timeline_api import router as kaspi_seller_timeline_router
from .marketplace_api import router as marketplace_router
from .marketplace_orders_api import router as marketplace_orders_router
from .monitoring_api import router as monitoring_router
from .monitoring_center_api import router as monitoring_center_router
from .pricing_api import router as pricing_router
from .product_commerce_api import router as product_commerce_router
from .product_detail_api import router as product_detail_router
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

APP_VERSION = "0.15.0"
DEPLOYMENT_MARKER = "continuous-kaspi-order-snapshots-v1"
STATIC_DIR = Path(__file__).resolve().parent / "static"
KASPI_ORDER_DISPATCH_INTERVAL_SECONDS = 60
KASPI_ORDER_SNAPSHOT_REFRESH_SECONDS = 180

app = FastAPI(
    title="LEO CRM API",
    version=APP_VERSION,
    description="Backend for product monitoring, pricing, XML, orders and purchases.",
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(ui_router)
app.include_router(products_router)
app.include_router(product_detail_router)
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
app.include_router(kaspi_seller_timeline_router)
app.include_router(pricing_router)
app.include_router(marketplace_router)
app.include_router(marketplace_orders_router)
app.include_router(commerce_router)
app.include_router(product_identity_router)
app.include_router(purchase_router)

_kaspi_dispatch_task: asyncio.Task | None = None


async def _continuous_kaspi_order_dispatch() -> None:
    while True:
        try:
            with SessionLocal() as db:
                dispatch_stale_kaspi_orders(
                    db,
                    limit=200,
                    refresh_seconds=KASPI_ORDER_SNAPSHOT_REFRESH_SECONDS,
                )
                db.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            # A temporary database or schema failure must not stop the web service.
            # The next cycle retries automatically.
            pass
        await asyncio.sleep(KASPI_ORDER_DISPATCH_INTERVAL_SECONDS)


@app.on_event("startup")
async def start_continuous_kaspi_dispatch() -> None:
    global _kaspi_dispatch_task
    if _kaspi_dispatch_task is None or _kaspi_dispatch_task.done():
        _kaspi_dispatch_task = asyncio.create_task(_continuous_kaspi_order_dispatch())


@app.on_event("shutdown")
async def stop_continuous_kaspi_dispatch() -> None:
    global _kaspi_dispatch_task
    if _kaspi_dispatch_task is None:
        return
    _kaspi_dispatch_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _kaspi_dispatch_task
    _kaspi_dispatch_task = None


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "service": "leo-crm",
        "status": "running",
        "version": APP_VERSION,
        "deployment_marker": DEPLOYMENT_MARKER,
        "docs": "/docs",
        "crm": "/crm",
    }


@app.get("/health")
async def health() -> dict[str, str]:
    """Cheap liveness probe used by Render.

    This endpoint intentionally does not acquire a database connection. A busy
    database pool must not make Render restart an otherwise healthy web process.
    """

    return {
        "status": "ok",
        "database": "not_checked",
        "version": APP_VERSION,
        "deployment_marker": DEPLOYMENT_MARKER,
        "timestamp": datetime.now(UTC).isoformat(),
    }


@app.get("/ready")
async def ready():
    """Readiness probe that verifies PostgreSQL without crashing the service."""

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
