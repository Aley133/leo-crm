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
from backend.app.observation_engine import record_failed_attempt, record_successful_observation
from backend.app.supplier_adapters.base import AdapterRequest, NormalizedOffer, SupplierAdapter
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct


def _seed_target(session: Session) -> tuple[MonitorTarget, SupplierProduct]:
    product = Product(kaspi_product_id="OBS-001", merchant_sku="OBS-001", name="Observation product")
    supplier = Supplier(code="fake-observation", name="Fake Observation")
    session.add_all([product, supplier])
    session.flush()

    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="FAKE-001",
        title="Fake supplier product",
        url="https://example.com/fake/1",
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


def _offer(
    supplier_product_id: int,
    *,
    price: str = "5640.00",
    delivery_days: int = 2,
    seller: str = " Example Seller ",
    observed_at: datetime | None = None,
) -> NormalizedOffer:
    return NormalizedOffer(
        supplier_product_id=supplier_product_id,
        price=Decimal(price),
        old_price=Decimal("6200.00"),
        available=True,
        stock=None,
        delivery_days=delivery_days,
        seller=seller,
        adapter_schema_version="fake-v1",
        observed_at=observed_at or datetime(2026, 7, 19, 10, 0, 2, tzinfo=UTC),
        raw_metadata={"source": "fixture", "layout": 1},
    )


def test_first_success_creates_attempt_state_and_observation(db_session: Session) -> None:
    target, supplier_product = _seed_target(db_session)
    started_at = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    finished_at = started_at + timedelta(seconds=3)

    result = record_successful_observation(
        db_session,
        monitor_target_id=target.id,
        lease_token="lease-token-a",
        adapter_code="fake-v1",
        access_strategy="fixture",
        started_at=started_at,
        finished_at=finished_at,
        offer=_offer(supplier_product.id),
    )

    assert result.changed is True
    assert result.state_version == 1
    assert result.observation_id is not None

    attempt = db_session.get(MonitorAttempt, result.attempt_id)
    state = db_session.scalar(
        select(SupplierOfferState).where(SupplierOfferState.supplier_product_id == supplier_product.id)
    )
    observation = db_session.get(SupplierOfferObservation, result.observation_id)

    assert attempt is not None
    assert attempt.outcome == AttemptOutcome.SUCCESS.value
    assert attempt.duration_ms == 3000
    assert state is not None
    assert state.price == Decimal("5640.00")
    assert state.version == 1
    assert observation is not None
    assert observation.monitor_attempt_id == attempt.id
    assert observation.fingerprint == state.fingerprint


def test_identical_success_updates_last_checked_without_new_observation(db_session: Session) -> None:
    target, supplier_product = _seed_target(db_session)
    started_at = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    offer = _offer(supplier_product.id)

    first = record_successful_observation(
        db_session,
        monitor_target_id=target.id,
        lease_token="lease-token-a",
        adapter_code="fake-v1",
        access_strategy="fixture",
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=2),
        offer=offer,
    )
    second_finished = started_at + timedelta(minutes=5)
    second = record_successful_observation(
        db_session,
        monitor_target_id=target.id,
        lease_token="lease-token-a",
        adapter_code="fake-v1",
        access_strategy="fixture",
        started_at=second_finished - timedelta(seconds=1),
        finished_at=second_finished,
        offer=_offer(supplier_product.id, seller="example   seller"),
    )

    state = db_session.scalar(
        select(SupplierOfferState).where(SupplierOfferState.supplier_product_id == supplier_product.id)
    )
    observation_count = db_session.scalar(select(func.count()).select_from(SupplierOfferObservation))
    attempt_count = db_session.scalar(select(func.count()).select_from(MonitorAttempt))

    assert first.changed is True
    assert second.changed is False
    assert second.observation_id is None
    assert state is not None
    assert state.version == 1
    assert state.last_checked_at.replace(tzinfo=UTC) == second_finished
    assert observation_count == 1
    assert attempt_count == 2


def test_meaningful_change_creates_new_observation_and_increments_version(db_session: Session) -> None:
    target, supplier_product = _seed_target(db_session)
    started_at = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)

    first = record_successful_observation(
        db_session,
        monitor_target_id=target.id,
        lease_token="lease-token-a",
        adapter_code="fake-v1",
        access_strategy="fixture",
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=2),
        offer=_offer(supplier_product.id),
    )
    second = record_successful_observation(
        db_session,
        monitor_target_id=target.id,
        lease_token="lease-token-a",
        adapter_code="fake-v1",
        access_strategy="fixture",
        started_at=started_at + timedelta(minutes=5),
        finished_at=started_at + timedelta(minutes=5, seconds=2),
        offer=_offer(supplier_product.id, price="5900.00", delivery_days=4),
    )

    state = db_session.scalar(
        select(SupplierOfferState).where(SupplierOfferState.supplier_product_id == supplier_product.id)
    )
    observations = list(
        db_session.scalars(
            select(SupplierOfferObservation).order_by(SupplierOfferObservation.id)
        ).all()
    )

    assert first.changed is True
    assert second.changed is True
    assert second.state_version == 2
    assert second.observation_id is not None
    assert state is not None
    assert state.price == Decimal("5900.00")
    assert state.delivery_days == 4
    assert state.version == 2
    assert len(observations) == 2
    assert observations[0].fingerprint != observations[1].fingerprint


def test_failed_attempt_does_not_create_or_change_offer_state(db_session: Session) -> None:
    target, supplier_product = _seed_target(db_session)
    started_at = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)

    attempt_id = record_failed_attempt(
        db_session,
        monitor_target_id=target.id,
        lease_token="lease-token-a",
        adapter_code="fake-v1",
        access_strategy="fixture",
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=30),
        outcome=AttemptOutcome.TIMEOUT,
        error_code="adapter_timeout",
        error_message="Fake adapter timed out",
    )

    attempt = db_session.get(MonitorAttempt, attempt_id)
    state = db_session.scalar(
        select(SupplierOfferState).where(SupplierOfferState.supplier_product_id == supplier_product.id)
    )

    assert attempt is not None
    assert attempt.outcome == AttemptOutcome.TIMEOUT.value
    assert state is None


def test_offer_rejects_invalid_business_values() -> None:
    with pytest.raises(ValueError, match="price must not be negative"):
        NormalizedOffer(
            supplier_product_id=1,
            price=Decimal("-1"),
            old_price=None,
            available=True,
            stock=None,
            delivery_days=1,
            seller=None,
            adapter_schema_version="v1",
            observed_at=datetime(2026, 7, 19, tzinfo=UTC),
        )


class FakeAdapter:
    code = "fake-v1"
    access_strategy = "fixture"

    def __init__(self, offer: NormalizedOffer) -> None:
        self.offer = offer

    async def fetch(self, request: AdapterRequest) -> NormalizedOffer:
        assert request.supplier_product_id == self.offer.supplier_product_id
        return self.offer


def test_fake_adapter_satisfies_protocol_contract() -> None:
    offer = _offer(1)
    adapter: SupplierAdapter = FakeAdapter(offer)

    assert adapter.code == "fake-v1"
    assert adapter.access_strategy == "fixture"
