from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.models import Product
from backend.app.monitoring import (
    AttemptOutcome,
    MonitorAttempt,
    MonitorStatus,
    MonitorTarget,
    SupplierOfferObservation,
    SupplierOfferState,
)
from backend.app.observation_engine import (
    StaleLeaseError,
    record_failed_attempt,
    record_successful_observation,
)
from backend.app.supplier_adapters.base import NormalizedOffer
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct


def _seed_reclaimed_target(session: Session) -> tuple[MonitorTarget, SupplierProduct]:
    product = Product(
        kaspi_product_id="OBS-STALE-001",
        merchant_sku="OBS-STALE-001",
        name="Stale observation product",
    )
    supplier = Supplier(code="fake-stale", name="Fake Stale")
    session.add_all([product, supplier])
    session.flush()

    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="FAKE-STALE-001",
        title="Fake stale supplier product",
        url="https://example.com/fake/stale/1",
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
        lease_owner="worker-b",
        lease_token="current-token-b",
        lease_until=datetime(2026, 7, 19, 10, 5, tzinfo=UTC),
    )
    session.add(target)
    session.commit()
    return target, supplier_product


def _offer(supplier_product_id: int) -> NormalizedOffer:
    return NormalizedOffer(
        supplier_product_id=supplier_product_id,
        price=Decimal("5640.00"),
        old_price=Decimal("6200.00"),
        available=True,
        stock=None,
        delivery_days=2,
        seller="Example Seller",
        adapter_schema_version="fake-v1",
        observed_at=datetime(2026, 7, 19, 10, 0, 2, tzinfo=UTC),
    )


def test_stale_worker_cannot_record_successful_observation(db_session: Session) -> None:
    target, supplier_product = _seed_reclaimed_target(db_session)
    started_at = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)

    with pytest.raises(StaleLeaseError):
        record_successful_observation(
            db_session,
            monitor_target_id=target.id,
            lease_token="stale-token-a",
            adapter_code="fake-v1",
            access_strategy="fixture",
            started_at=started_at,
            finished_at=started_at + timedelta(seconds=2),
            offer=_offer(supplier_product.id),
        )

    assert db_session.scalar(select(func.count()).select_from(MonitorAttempt)) == 0
    assert db_session.scalar(select(func.count()).select_from(SupplierOfferState)) == 0
    assert db_session.scalar(select(func.count()).select_from(SupplierOfferObservation)) == 0


def test_stale_worker_cannot_record_failed_attempt(db_session: Session) -> None:
    target, _ = _seed_reclaimed_target(db_session)
    started_at = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)

    with pytest.raises(StaleLeaseError):
        record_failed_attempt(
            db_session,
            monitor_target_id=target.id,
            lease_token="stale-token-a",
            adapter_code="fake-v1",
            access_strategy="fixture",
            started_at=started_at,
            finished_at=started_at + timedelta(seconds=30),
            outcome=AttemptOutcome.TIMEOUT,
            error_code="adapter_timeout",
        )

    assert db_session.scalar(select(func.count()).select_from(MonitorAttempt)) == 0
