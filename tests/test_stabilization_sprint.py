from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.models import Product
from backend.app.monitoring import (
    AttemptOutcome,
    MonitorAttempt,
    MonitorTarget,
    SourceHealth,
    SupplierOfferObservation,
)
from backend.app.observation_engine import record_successful_observation
from backend.app.scheduler_engine import AdapterRegistry, process_claimed_target
from backend.app.source_health_engine import record_source_failure, record_source_success
from backend.app.supplier_adapters.base import AdapterRequest, NormalizedOffer
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct
from backend.app.lease_engine import LeaseClaim


def _seed(session: Session) -> tuple[MonitorTarget, Supplier, SupplierProduct]:
    product = Product(kaspi_product_id="STAB-1", merchant_sku="STAB-1", name="Stabilization")
    supplier = Supplier(code="stab", name="Stabilization supplier")
    session.add_all([product, supplier])
    session.flush()
    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="STAB-1",
        title="Stabilization product",
        url="https://example.com/stab/1",
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
        interval_seconds=300,
        next_check_at=datetime(2026, 7, 19, 10, 0, tzinfo=UTC),
        lease_owner="worker-a",
        lease_token="lease-a",
        lease_until=datetime(2026, 7, 19, 10, 5, tzinfo=UTC),
    )
    session.add(target)
    session.commit()
    return target, supplier, supplier_product


def _offer(product_id: int, price: str, minute: int) -> NormalizedOffer:
    return NormalizedOffer(
        supplier_product_id=product_id,
        price=Decimal(price),
        old_price=None,
        available=True,
        stock=10,
        delivery_days=2,
        seller="Seller",
        adapter_schema_version="test-v1",
        observed_at=datetime(2026, 7, 19, 10, minute, tzinfo=UTC),
    )


def test_a_b_a_creates_three_history_events(db_session: Session) -> None:
    target, _, supplier_product = _seed(db_session)
    base = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)

    first = record_successful_observation(
        db_session,
        monitor_target_id=target.id,
        lease_token="lease-a",
        adapter_code="test",
        access_strategy="fixture",
        started_at=base,
        finished_at=base + timedelta(seconds=1),
        offer=_offer(supplier_product.id, "100.00", 0),
    )
    second = record_successful_observation(
        db_session,
        monitor_target_id=target.id,
        lease_token="lease-a",
        adapter_code="test",
        access_strategy="fixture",
        started_at=base + timedelta(minutes=1),
        finished_at=base + timedelta(minutes=1, seconds=1),
        offer=_offer(supplier_product.id, "120.00", 1),
    )
    third = record_successful_observation(
        db_session,
        monitor_target_id=target.id,
        lease_token="lease-a",
        adapter_code="test",
        access_strategy="fixture",
        started_at=base + timedelta(minutes=2),
        finished_at=base + timedelta(minutes=2, seconds=1),
        offer=_offer(supplier_product.id, "100.00", 2),
    )

    observations = list(
        db_session.scalars(
            select(SupplierOfferObservation).order_by(SupplierOfferObservation.id)
        ).all()
    )
    assert first.changed and second.changed and third.changed
    assert len(observations) == 3
    assert observations[0].fingerprint == observations[2].fingerprint
    assert observations[0].id != observations[2].id


def test_source_health_backoff_and_recovery(db_session: Session) -> None:
    _, supplier, _ = _seed(db_session)
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    health = record_source_failure(
        db_session,
        supplier_id=supplier.id,
        outcome=AttemptOutcome.CAPTCHA,
        error_code="captcha_detected",
        finished_at=now,
    )
    db_session.commit()
    assert health.blocked_until is not None
    assert health.blocked_until > now
    assert health.consecutive_failures == 1

    recovered = record_source_success(
        db_session,
        supplier_id=supplier.id,
        finished_at=now + timedelta(hours=3),
    )
    db_session.commit()
    assert recovered.blocked_until is None
    assert recovered.consecutive_failures == 0
    assert db_session.scalar(select(func.count()).select_from(SourceHealth)) == 1


class _Adapter:
    code = "stab"
    access_strategy = "fixture"

    def __init__(self, offer: NormalizedOffer) -> None:
        self.offer = offer

    async def fetch(self, request: AdapterRequest) -> NormalizedOffer:
        return self.offer


def test_scheduler_rolls_back_observation_when_reschedule_fails(
    db_session: Session,
    monkeypatch,
) -> None:
    target, _, supplier_product = _seed(db_session)
    bind = db_session.get_bind()
    factory = sessionmaker(bind=bind, expire_on_commit=False)
    claim = LeaseClaim(
        target_id=target.id,
        product_binding_id=target.product_binding_id,
        lease_owner="worker-a",
        lease_token="lease-a",
        lease_until=target.lease_until,
    )
    registry = AdapterRegistry({"stab": _Adapter(_offer(supplier_product.id, "100.00", 0))})

    def explode(*args, **kwargs):
        raise RuntimeError("reschedule failed")

    monkeypatch.setattr("backend.app.scheduler_engine.reschedule_success", explode)
    result = asyncio.run(
        process_claimed_target(
            claim,
            registry=registry,
            session_factory=factory,
            now_factory=lambda: datetime(2026, 7, 19, 10, 0, 1, tzinfo=UTC),
        )
    )

    db_session.expire_all()
    assert result.status == "failed"
    assert db_session.scalar(select(func.count()).select_from(SupplierOfferObservation)) == 0
    successful_attempts = db_session.scalar(
        select(func.count()).select_from(MonitorAttempt).where(
            MonitorAttempt.outcome == AttemptOutcome.SUCCESS.value
        )
    )
    assert successful_attempts == 0
