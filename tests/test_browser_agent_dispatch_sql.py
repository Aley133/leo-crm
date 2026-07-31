from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects import postgresql

from backend.app import browser_agent_api
from backend.app.browser_agent_dispatch import (
    build_due_browser_targets_statement,
    dispatch_due_browser_targets,
)
from backend.app.models import Product
from backend.app.monitoring import MonitorTarget
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct


def test_due_browser_dispatch_compiles_for_postgresql_skip_locked() -> None:
    statement = build_due_browser_targets_statement(limit=100, supplier_code="OZON")
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()

    assert "FOR UPDATE OF MONITOR_TARGETS SKIP LOCKED" in sql
    assert "NOT IN" in sql
    assert "BROWSER_AGENT_JOBS.MONITOR_TARGET_ID IS NOT NULL" in sql
    assert "SUPPLIERS.CODE = 'OZON'" in sql


def test_dispatch_recovers_target_left_in_legacy_long_backoff(db_session) -> None:
    now = datetime.now(UTC)
    product = Product(
        kaspi_product_id="151877903",
        name="GLS Pharmaceuticals Аргинин",
        status="active",
    )
    supplier = Supplier(code="ozon", name="Ozon")
    db_session.add_all([product, supplier])
    db_session.flush()
    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="51853964",
        title=product.name,
        url="https://www.ozon.ru/product/arginin-51853964/",
    )
    db_session.add(supplier_product)
    db_session.flush()
    binding = ProductBinding(
        product_id=product.id,
        supplier_product_id=supplier_product.id,
        status="active",
    )
    db_session.add(binding)
    db_session.flush()
    target = MonitorTarget(
        product_binding_id=binding.id,
        status="active",
        interval_seconds=300,
        next_check_at=now + timedelta(hours=2),
        last_checked_at=now - timedelta(hours=12),
        consecutive_failures=7,
    )
    db_session.add(target)
    db_session.flush()

    result = dispatch_due_browser_targets(
        db_session,
        supplier_code="ozon",
    )

    assert result.queued_count == 1


def test_duplicate_dispatchers_share_one_short_database_scan_window() -> None:
    browser_agent_api._DISPATCH_LAST_AT.clear()
    try:
        acquired, retry_after = browser_agent_api._acquire_dispatch_slot(
            "OZON",
            now=100.0,
        )
        duplicate, duplicate_retry_after = browser_agent_api._acquire_dispatch_slot(
            "ozon",
            now=101.0,
        )
        other_supplier, _ = browser_agent_api._acquire_dispatch_slot(
            "wb",
            now=101.0,
        )
        next_window, _ = browser_agent_api._acquire_dispatch_slot(
            "ozon",
            now=100.0 + browser_agent_api.DISPATCH_MIN_INTERVAL_SECONDS,
        )
    finally:
        browser_agent_api._DISPATCH_LAST_AT.clear()

    assert acquired is True
    assert retry_after == 0
    assert duplicate is False
    assert duplicate_retry_after > 0
    assert other_supplier is True
    assert next_window is True
