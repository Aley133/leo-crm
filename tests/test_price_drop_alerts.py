from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import OutboxEvent, Product
from backend.app.monitoring import MonitorStatus, MonitorTarget
from backend.app.observation_engine import record_successful_observation
from backend.app.price_drop_alerts import PRICE_DROP_EVENT_TYPE
from backend.app.supplier_adapters.base import NormalizedOffer
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct


STARTED_AT = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)


def _seed_target(
    session: Session,
    *,
    price_alert_enabled: bool = True,
) -> tuple[MonitorTarget, SupplierProduct]:
    product = Product(
        kaspi_product_id="PRICE-DROP-001",
        merchant_sku="SKU-DROP-001",
        name="Выгодный товар",
        sudden_price_alert_enabled=price_alert_enabled,
    )
    supplier = Supplier(code="ozon", name="Ozon")
    session.add_all([product, supplier])
    session.flush()
    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="OZON-DROP-001",
        title="Выгодный товар на Ozon",
        url="https://www.ozon.ru/product/price-drop-001/",
    )
    session.add(supplier_product)
    session.flush()
    binding = ProductBinding(
        product_id=product.id,
        supplier_product_id=supplier_product.id,
        status="active",
        is_primary=True,
    )
    session.add(binding)
    session.flush()
    target = MonitorTarget(
        product_binding_id=binding.id,
        status=MonitorStatus.ACTIVE.value,
        interval_seconds=300,
        next_check_at=STARTED_AT,
        lease_owner="worker-alert",
        lease_token="lease-alert",
        lease_until=STARTED_AT + timedelta(hours=2),
    )
    session.add(target)
    session.commit()
    return target, supplier_product


def _record(
    session: Session,
    *,
    target: MonitorTarget,
    supplier_product: SupplierProduct,
    price: str,
    minute: int,
    available: bool | None = True,
    stock: int | None = 5,
    delivery_days: int = 2,
) -> None:
    observed_at = STARTED_AT + timedelta(minutes=minute)
    record_successful_observation(
        session,
        monitor_target_id=target.id,
        lease_token="lease-alert",
        adapter_code="ozon-browser-agent-v2",
        access_strategy="browser",
        started_at=observed_at,
        finished_at=observed_at + timedelta(seconds=2),
        offer=NormalizedOffer(
            supplier_product_id=supplier_product.id,
            price=Decimal(price),
            old_price=None,
            currency="KZT",
            available=available,
            stock=stock,
            delivery_days=delivery_days,
            seller="Ozon",
            adapter_schema_version="ozon-browser-structured-v4",
            observed_at=observed_at,
        ),
    )


def _alerts(session: Session) -> list[OutboxEvent]:
    return list(
        session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.event_type == PRICE_DROP_EVENT_TYPE)
            .order_by(OutboxEvent.created_at, OutboxEvent.id)
        ).all()
    )


def test_six_price_baseline_detects_a_sudden_ozon_drop(db_session: Session) -> None:
    target, supplier_product = _seed_target(db_session)
    for minute, price in enumerate(("3000", "3200", "3100", "2900", "2700", "3100")):
        _record(
            db_session,
            target=target,
            supplier_product=supplier_product,
            price=price,
            minute=minute,
        )

    _record(
        db_session,
        target=target,
        supplier_product=supplier_product,
        price="1000",
        minute=6,
    )

    alerts = _alerts(db_session)
    assert len(alerts) == 1
    assert alerts[0].payload_json == {
        "version": 1,
        "supplier_product_id": supplier_product.id,
        "observation_id": alerts[0].payload_json["observation_id"],
        "baseline_price": "3050.00",
        "current_price": "1000.00",
        "drop_percent": "67.2",
        "currency": "KZT",
        "baseline_sample_size": 6,
        "observed_at": (STARTED_AT + timedelta(minutes=6)).isoformat(),
        "product_id": alerts[0].payload_json["product_id"],
        "product_name": "Выгодный товар",
        "merchant_sku": "SKU-DROP-001",
        "kaspi_product_id": "PRICE-DROP-001",
        "supplier_code": "ozon",
        "supplier_name": "Ozon",
        "supplier_product_title": "Выгодный товар на Ozon",
        "supplier_product_url": "https://www.ozon.ru/product/price-drop-001/",
        "binding_id": alerts[0].payload_json["binding_id"],
    }


def test_product_must_explicitly_opt_in_to_price_drop_alerts(
    db_session: Session,
) -> None:
    target, supplier_product = _seed_target(
        db_session,
        price_alert_enabled=False,
    )
    _record(
        db_session,
        target=target,
        supplier_product=supplier_product,
        price="3000",
        minute=0,
    )
    _record(
        db_session,
        target=target,
        supplier_product=supplier_product,
        price="1000",
        minute=1,
    )

    assert _alerts(db_session) == []


def test_drop_below_fifty_percent_does_not_alert(db_session: Session) -> None:
    target, supplier_product = _seed_target(db_session)
    for minute, price in enumerate(("3000", "3200", "3100", "2900", "2700", "3100")):
        _record(
            db_session,
            target=target,
            supplier_product=supplier_product,
            price=price,
            minute=minute,
        )

    _record(
        db_session,
        target=target,
        supplier_product=supplier_product,
        price="1600",
        minute=6,
    )

    assert _alerts(db_session) == []


def test_exactly_fifty_percent_drop_alerts(db_session: Session) -> None:
    target, supplier_product = _seed_target(db_session)
    _record(
        db_session,
        target=target,
        supplier_product=supplier_product,
        price="3000",
        minute=0,
    )
    _record(
        db_session,
        target=target,
        supplier_product=supplier_product,
        price="1500",
        minute=1,
    )

    alerts = _alerts(db_session)
    assert len(alerts) == 1
    assert alerts[0].payload_json["drop_percent"] == "50.0"


def test_low_price_state_does_not_repeat_until_price_recovers(db_session: Session) -> None:
    target, supplier_product = _seed_target(db_session)
    _record(
        db_session,
        target=target,
        supplier_product=supplier_product,
        price="3000",
        minute=0,
    )
    _record(
        db_session,
        target=target,
        supplier_product=supplier_product,
        price="1000",
        minute=1,
    )
    _record(
        db_session,
        target=target,
        supplier_product=supplier_product,
        price="1000",
        minute=2,
        delivery_days=3,
    )
    assert len(_alerts(db_session)) == 1

    _record(
        db_session,
        target=target,
        supplier_product=supplier_product,
        price="3100",
        minute=3,
    )
    _record(
        db_session,
        target=target,
        supplier_product=supplier_product,
        price="900",
        minute=4,
    )
    assert len(_alerts(db_session)) == 2


def test_unavailable_or_zero_stock_offer_does_not_alert(db_session: Session) -> None:
    target, supplier_product = _seed_target(db_session)
    _record(
        db_session,
        target=target,
        supplier_product=supplier_product,
        price="3000",
        minute=0,
    )
    _record(
        db_session,
        target=target,
        supplier_product=supplier_product,
        price="1000",
        minute=1,
        available=False,
    )
    _record(
        db_session,
        target=target,
        supplier_product=supplier_product,
        price="900",
        minute=2,
        available=True,
        stock=0,
    )

    assert _alerts(db_session) == []
