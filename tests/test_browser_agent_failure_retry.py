from datetime import UTC, datetime, timedelta
from decimal import Decimal

from backend.app.browser_agent_failure import (
    MAX_BROWSER_FAILURE_RETRY_SECONDS,
    persist_browser_agent_failure,
)
from backend.app.browser_agent_models import BrowserAgentJob
from backend.app.models import Product
from backend.app.monitoring import MonitorStatus, MonitorTarget, SupplierOfferState
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def test_failed_browser_load_stays_active_and_retries_within_thirty_minutes(
    db_session,
) -> None:
    finished_at = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)
    supplier = Supplier(code="ozon", name="Ozon")
    product = Product(
        kaspi_product_id="123456789",
        merchant_sku="SKU-123456789",
        name="Товар с повтором",
        status="active",
    )
    db_session.add_all([supplier, product])
    db_session.flush()
    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="ozon-123",
        title="Карточка Ozon",
        url="https://www.ozon.ru/product/ozon-123/",
        current_price=Decimal("6377"),
        delivery_days=4,
        in_stock=True,
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
        status=MonitorStatus.ACTIVE.value,
        interval_seconds=300,
        next_check_at=finished_at,
        consecutive_failures=9,
    )
    db_session.add(target)
    db_session.flush()
    state = SupplierOfferState(
        supplier_product_id=supplier_product.id,
        price=Decimal("6377"),
        currency="KZT",
        available=True,
        stock=None,
        delivery_days=4,
        fingerprint="last-valid-offer",
        adapter_schema_version="ozon-http-session-v1",
        observed_at=finished_at - timedelta(minutes=5),
        last_checked_at=finished_at - timedelta(minutes=5),
    )
    db_session.add(state)
    db_session.flush()
    job = BrowserAgentJob(
        monitor_target_id=target.id,
        supplier_product_id=supplier_product.id,
        url=supplier_product.url,
        status="leased",
        created_at=finished_at - timedelta(seconds=20),
    )
    db_session.add(job)
    db_session.flush()

    persist_browser_agent_failure(
        db_session,
        job=job,
        error_code="AdapterParseError",
        error_message="Ozon page did not load",
        finished_at=finished_at,
    )

    assert target.status == MonitorStatus.ACTIVE.value
    assert target.consecutive_failures == 10
    assert _as_utc(target.next_check_at) == finished_at + timedelta(
        seconds=MAX_BROWSER_FAILURE_RETRY_SECONDS
    )
    assert supplier_product.current_price == Decimal("6377")
    assert supplier_product.delivery_days == 4
    assert supplier_product.in_stock is True
    assert state.price == Decimal("6377")
    assert state.available is True
    assert state.delivery_days == 4
