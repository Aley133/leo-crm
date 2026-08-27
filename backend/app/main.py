from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError, TimeoutError as SQLAlchemyTimeoutError

from . import legacy_workspace_scope as _legacy_workspace_scope  # noqa: F401
from .action_api import router as action_router
from .browser_agent_api import router as browser_agent_router
from .browser_agent_monitoring_api import router as browser_agent_monitoring_router
from .browser_agent_registry_api import router as browser_agent_registry_router
from .catalog_api import router as catalog_router
from .commerce.api import router as commerce_router
from .dashboard_api import router as dashboard_router
from .db import SessionLocal, engine
from .dumping_api import public_router as dumping_public_router
from .dumping_api import router as dumping_router
from .dumping_competitor_worker import (
    SCHEDULER_LAST_RUN as DUMPING_SCHEDULER_STATUS,
    start_dumping_competitor_worker,
    stop_dumping_competitor_worker,
)
from .dumping_run_compat_api import router as dumping_run_compat_router
from .fixed_procurement_source_api import router as fixed_procurement_source_router
from .fast_dumping_agent_api import router as fast_dumping_agent_router
from .fast_dumping_api import router as fast_dumping_router
from .inventory_api import router as inventory_router
from .kaspi_competitor_agent_api import router as kaspi_competitor_agent_router
from .kaspi_order_polling import ENRICHMENT_LAST_RUN as KASPI_ENRICHMENT_STATUS
from .kaspi_order_polling import LAST_RUN as KASPI_POLL_STATUS
from .kaspi_order_polling import MAINTENANCE_LAST_RUN as KASPI_MAINTENANCE_STATUS
from .kaspi_order_polling import (
    enrichment_polling_loop,
    maintenance_polling_loop,
    polling_loop,
)
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
from .product_test_api import agent_router as product_test_agent_router
from .product_test_api import router as product_test_router
from .product_xml_import_api import router as product_xml_import_router
from .products import router as products_router
from .purchase_api import router as purchase_router
from .retention_cleanup import LAST_RUN as RETENTION_CLEANUP_STATUS
from .retention_cleanup import retention_cleanup_loop
from .revenue_api import router as revenue_router
from .supplier_products_api import router as supplier_products_router
from .supplier_state_api import router as supplier_state_router
from .suppliers import router as suppliers_router
from .telegram_price_alerts import price_alert_publisher_loop
from .ui import router as ui_router
from .workspace_api import router as workspace_router
from .workspace_context import (
    LEGACY_WORKSPACE_ID,
    WORKSPACE_HEADER,
    reset_current_workspace_id,
    set_current_workspace_id,
)
from .workspace_kaspi import bootstrap_legacy_workspace_connection

APP_VERSION = "0.25.0"
DEPLOYMENT_MARKER = "http-monitoring-product-discovery"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="LEO CRM API",
    version=APP_VERSION,
    description="Backend for product monitoring, pricing, XML, orders and purchases.",
)

app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)


@app.middleware("http")
async def select_workspace(request: Request, call_next):
    raw_workspace_id = request.headers.get(WORKSPACE_HEADER, "").strip()
    try:
        workspace_id = LEGACY_WORKSPACE_ID if not raw_workspace_id else int(raw_workspace_id)
        if workspace_id < 1:
            raise ValueError
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"detail": f"{WORKSPACE_HEADER} must be a positive integer"},
        )

    token = set_current_workspace_id(workspace_id)
    try:
        return await call_next(request)
    finally:
        reset_current_workspace_id(token)


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
app.include_router(product_test_router)
app.include_router(product_test_agent_router)
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
app.include_router(kaspi_competitor_agent_router)
app.include_router(fast_dumping_agent_router)
app.include_router(pricing_router)
app.include_router(dumping_router)
app.include_router(fast_dumping_router)
app.include_router(dumping_run_compat_router)
app.include_router(dumping_public_router)
app.include_router(marketplace_router)
app.include_router(marketplace_orders_router)
app.include_router(commerce_router)
app.include_router(product_identity_router)
app.include_router(purchase_router)
app.include_router(revenue_router)
app.include_router(workspace_router)


def _process_rss_mb() -> float | None:
    """Read current RSS without allocating a database connection."""
    try:
        resident_pages = int(Path("/proc/self/statm").read_text(encoding="ascii").split()[1])
        return round(resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024), 1)
    except (IndexError, OSError, TypeError, ValueError):
        return None


def _database_pool_snapshot() -> dict[str, int | None]:
    """Expose local pool pressure without contacting PostgreSQL."""
    pool = engine.pool

    def metric(name: str) -> int | None:
        value = getattr(pool, name, None)
        if not callable(value):
            return None
        try:
            return int(value())
        except (TypeError, ValueError):
            return None

    return {
        "size": metric("size"),
        "checked_out": metric("checkedout"),
        "overflow": metric("overflow"),
    }


def _configure_thread_pool() -> int:
    """Bound synchronous request workers for a 512 MB Render instance."""
    raw = os.getenv("API_THREAD_LIMIT", "").strip()
    try:
        requested = int(raw) if raw else 12
    except ValueError:
        requested = 12
    limit = max(4, min(32, requested))
    anyio.to_thread.current_default_thread_limiter().total_tokens = limit
    return limit


def _bootstrap_workspace_connection() -> None:
    with SessionLocal() as session:
        with session.begin():
            bootstrap_legacy_workspace_connection(session)


def _database_is_ready() -> None:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))


@app.exception_handler(SQLAlchemyTimeoutError)
async def database_pool_timeout(
    _request: Request,
    _error: SQLAlchemyTimeoutError,
) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "2"},
        content={
            "detail": "CRM database is busy; retry shortly",
            "error_code": "database_pool_busy",
        },
    )


@app.on_event("startup")
async def start_background_services() -> None:
    app.state.thread_pool_limit = _configure_thread_pool()
    await asyncio.to_thread(_bootstrap_workspace_connection)

    stop_event = asyncio.Event()
    app.state.kaspi_poll_stop_event = stop_event
    app.state.kaspi_poll_task = asyncio.create_task(polling_loop(stop_event))
    app.state.kaspi_enrichment_task = asyncio.create_task(enrichment_polling_loop(stop_event))
    app.state.kaspi_maintenance_task = asyncio.create_task(maintenance_polling_loop(stop_event))

    price_alert_stop_event = asyncio.Event()
    app.state.price_alert_stop_event = price_alert_stop_event
    app.state.price_alert_task = asyncio.create_task(
        price_alert_publisher_loop(price_alert_stop_event)
    )

    retention_stop_event = asyncio.Event()
    app.state.retention_cleanup_stop_event = retention_stop_event
    app.state.retention_cleanup_task = asyncio.create_task(
        retention_cleanup_loop(retention_stop_event)
    )

    # Render schedules durable competitor jobs only. The local Kaspi Competitor
    # Agent remains the sole owner of Kaspi HTTP scans.
    await start_dumping_competitor_worker()


@app.on_event("shutdown")
async def stop_background_services() -> None:
    await stop_dumping_competitor_worker()

    stop_event = getattr(app.state, "kaspi_poll_stop_event", None)
    tasks = [
        getattr(app.state, "kaspi_poll_task", None),
        getattr(app.state, "kaspi_enrichment_task", None),
        getattr(app.state, "kaspi_maintenance_task", None),
    ]
    if stop_event is not None:
        stop_event.set()
    for task in tasks:
        if task is not None:
            task.cancel()
    await asyncio.gather(
        *(task for task in tasks if task is not None),
        return_exceptions=True,
    )

    price_alert_stop_event = getattr(app.state, "price_alert_stop_event", None)
    price_alert_task = getattr(app.state, "price_alert_task", None)
    if price_alert_stop_event is not None:
        price_alert_stop_event.set()
    if price_alert_task is not None:
        price_alert_task.cancel()
        try:
            await price_alert_task
        except asyncio.CancelledError:
            pass

    retention_stop_event = getattr(app.state, "retention_cleanup_stop_event", None)
    retention_task = getattr(app.state, "retention_cleanup_task", None)
    if retention_stop_event is not None:
        retention_stop_event.set()
    if retention_task is not None:
        retention_task.cancel()
        try:
            await retention_task
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
        "kaspi_feed": "/feeds/kaspi/catalog.xml",
        "kaspi_polling": dict(KASPI_POLL_STATUS),
        "kaspi_order_enrichment": dict(KASPI_ENRICHMENT_STATUS),
        "kaspi_order_maintenance": dict(KASPI_MAINTENANCE_STATUS),
        "dumping_scheduler": dict(DUMPING_SCHEDULER_STATUS),
        "data_retention": dict(RETENTION_CLEANUP_STATUS),
    }


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "database": "not_checked",
        "memory_rss_mb": _process_rss_mb(),
        "database_pool": _database_pool_snapshot(),
        "thread_pool_limit": getattr(app.state, "thread_pool_limit", None),
        "version": APP_VERSION,
        "deployment_marker": DEPLOYMENT_MARKER,
        "timestamp": datetime.now(UTC).isoformat(),
        "kaspi_polling": dict(KASPI_POLL_STATUS),
        "kaspi_order_enrichment": dict(KASPI_ENRICHMENT_STATUS),
        "kaspi_order_maintenance": dict(KASPI_MAINTENANCE_STATUS),
        "dumping_scheduler": dict(DUMPING_SCHEDULER_STATUS),
        "data_retention": dict(RETENTION_CLEANUP_STATUS),
    }


@app.get("/ready")
async def ready():
    try:
        await asyncio.to_thread(_database_is_ready)
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
