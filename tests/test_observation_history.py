from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import Product
from backend.app.monitoring import MonitorStatus, MonitorTarget, SupplierOfferObservation, SupplierOfferState
from backend.app.observation_engine import persist_successful_observation
from backend.app.supplier_adapters.base import NormalizedOffer
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct


def _seed_claimed_target(session: Session) -> tuple[MonitorTarget, SupplierProduct]:
    supplier = Supplier(code="history-test", name="History Test")
    product = Product(
        kaspi_product_id="HISTORY-1",
        merchant_sku="HISTORY-1",
        name="History product",
    )
    session.add_all([supplier, product])
    session.flush()

    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="HISTORY-EXT-1",
        title="History supplier product",
        url="https://example.com/history-1",
    )
    session.add(supplier_product)
    session.flush()

    binding = ProductBinding(
        product_id=product.id,
        supplier_product_id=supplier_product.id,
        status="active",
    )
    session.add(binding)
    session.flush()

    target = MonitorTarget(
        product_binding_id=binding.id,
        status=MonitorStatus.ACTIVE.value,
        interval_seconds=300,
        next_check_at=datetime(2026, 7, 19, 10, 0, tzinfo=UTC),
        lease_owner="worker-history",
        lease_token="history-lease-token",
        lease_until=datetime(2026, 7, 19, 10, 10, tzinfo=UTC),
    )
    session.add(target)
    session.commit()
    return target, supplier_product


def _offer(
    supplier_product_id: int,
    *,
    price: str,
    observed_at: datetime,
) -> NormalizedOffer:
    return NormalizedOffer(
        supplier_product_id=supplier_product_id,
        price=Decimal(price),
        old_price=None,
        available=True,
        stock=3,
        delivery_days=2,
        seller="Fixture Seller",
        adapter_schema_version="history-v1",
        observed_at=observed_at,
    )


def test_a_b_a_creates_three_historical_observations(db_session: Session) -> None:
    target, supplier_product = _seed_claimed_target(db_session)
    started_at = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)

    first = persist_successful_observation(
        db_session,
        monitor_target_id=target.id,
        lease_token="history-lease-token",
        adapter_code="history-adapter-v1",
        access_strategy="fixture",
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
        offer=_offer(
            supplier_product.id,
            price="5000.00",
            observed_at=started_at + timedelta(seconds=1),
        ),
    )
    db_session.commit()

    second = persist_successful_observation(
        db_session,
        monitor_target_id=target.id,
        lease_token="history-lease-token",
        adapter_code="history-adapter-v1",
        access_strategy="fixture",
        started_at=started_at + timedelta(minutes=1),
        finished_at=started_at + timedelta(minutes=1, seconds=1),
        offer=_offer(
            supplier_product.id,
            price="6000.00",
            observed_at=started_at + timedelta(minutes=1, seconds=1),
        ),
    )
    db_session.commit()

    third = persist_successful_observation(
        db_session,
        monitor_target_id=target.id,
        lease_token="history-lease-token",
        adapter_code="history-adapter-v1",
        access_strategy="fixture",
        started_at=started_at + timedelta(minutes=2),
        finished_at=started_at + timedelta(minutes=2, seconds=1),
        offer=_offer(
            supplier_product.id,
            price="5000.00",
            observed_at=started_at + timedelta(minutes=2, seconds=1),
        ),
    )
    db_session.commit()

    observations = list(
        db_session.scalars(
            select(SupplierOfferObservation).order_by(SupplierOfferObservation.id)
        ).all()
    )
    state = db_session.scalar(
        select(SupplierOfferState).where(
            SupplierOfferState.supplier_product_id == supplier_product.id
        )
    )

    assert first.changed is True
    assert second.changed is True
    assert third.changed is True
    assert len(observations) == 3
    assert [item.price for item in observations] == [
        Decimal("5000.00"),
        Decimal("6000.00"),
        Decimal("5000.00"),
    ]
    assert len({item.monitor_attempt_id for item in observations}) == 3
    assert observations[0].fingerprint == observations[2].fingerprint
    assert observations[0].id != observations[2].id
    assert state is not None
    assert state.price == Decimal("5000.00")
    assert state.version == 3
