from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

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
from backend.app.observation_engine import persist_failed_attempt, persist_successful_observation
from backend.app.supplier_adapters.base import NormalizedOffer
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct


def _seed_claimed_target(session: Session) -> tuple[MonitorTarget, SupplierProduct]:
    supplier = Supplier(code="tx-test", name="Transaction Test")
    product = Product(
        kaspi_product_id="TX-1",
        merchant_sku="TX-1",
        name="Transaction boundary product",
    )
    session.add_all([supplier, product])
    session.flush()

    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="TX-EXT-1",
        title="Transaction supplier product",
        url="https://example.com/tx-1",
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
        lease_owner="worker-a",
        lease_token="lease-token-a",
        lease_until=datetime(2026, 7, 19, 10, 5, tzinfo=UTC),
    )
    session.add(target)
    session.commit()
    return target, supplier_product


def test_success_persistence_does_not_commit_its_own_transaction(db_session: Session) -> None:
    target, supplier_product = _seed_claimed_target(db_session)
    started_at = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    finished_at = datetime(2026, 7, 19, 10, 0, 1, tzinfo=UTC)

    persist_successful_observation(
        db_session,
        monitor_target_id=target.id,
        lease_token="lease-token-a",
        adapter_code="tx-adapter-v1",
        access_strategy="fixture",
        started_at=started_at,
        finished_at=finished_at,
        offer=NormalizedOffer(
            supplier_product_id=supplier_product.id,
            price=Decimal("5000.00"),
            old_price=None,
            available=True,
            stock=3,
            delivery_days=2,
            seller="Fixture Seller",
            adapter_schema_version="tx-v1",
            observed_at=finished_at,
        ),
    )

    db_session.rollback()

    assert db_session.scalar(select(func.count()).select_from(MonitorAttempt)) == 0
    assert db_session.scalar(select(func.count()).select_from(SupplierOfferState)) == 0
    assert db_session.scalar(select(func.count()).select_from(SupplierOfferObservation)) == 0


def test_failure_persistence_does_not_commit_its_own_transaction(db_session: Session) -> None:
    target, _ = _seed_claimed_target(db_session)
    started_at = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    finished_at = datetime(2026, 7, 19, 10, 0, 1, tzinfo=UTC)

    persist_failed_attempt(
        db_session,
        monitor_target_id=target.id,
        lease_token="lease-token-a",
        adapter_code="tx-adapter-v1",
        access_strategy="fixture",
        started_at=started_at,
        finished_at=finished_at,
        outcome=AttemptOutcome.TIMEOUT,
        error_code="fixture_timeout",
        error_message="fixture timeout",
    )

    db_session.rollback()

    assert db_session.scalar(select(func.count()).select_from(MonitorAttempt)) == 0
