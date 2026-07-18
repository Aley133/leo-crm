from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.models import Product
from backend.app.monitoring import AttemptOutcome, MonitorAttempt, MonitorStatus, MonitorTarget, SupplierOfferState
from backend.app.scheduler_engine import AdapterRegistry, run_scheduler_tick
from backend.app.supplier_adapters.base import AdapterRequest, NormalizedOffer
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct


def _session_factory(session: Session):
    return sessionmaker(bind=session.get_bind(), expire_on_commit=False)


def _seed_targets(session: Session, *, count: int, supplier_code: str = "fake-scheduler") -> list[MonitorTarget]:
    supplier = Supplier(code=supplier_code, name="Fake Scheduler")
    session.add(supplier)
    session.flush()
    targets: list[MonitorTarget] = []
    for index in range(count):
        product = Product(
            kaspi_product_id=f"SCHED-{index}",
            merchant_sku=f"SCHED-{index}",
            name=f"Scheduler product {index}",
        )
        session.add(product)
        session.flush()
        supplier_product = SupplierProduct(
            supplier_id=supplier.id,
            external_id=f"EXT-{index}",
            title=f"Supplier product {index}",
            url=f"https://example.com/{index}",
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
            next_check_at=datetime(2026, 7, 19, 9, 59, tzinfo=UTC),
        )
        session.add(target)
        targets.append(target)
    session.commit()
    return targets


class SuccessAdapter:
    code = "fake-success-v1"
    access_strategy = "fixture"

    async def fetch(self, request: AdapterRequest) -> NormalizedOffer:
        return NormalizedOffer(
            supplier_product_id=request.supplier_product_id,
            price=Decimal("5000.00") + request.supplier_product_id,
            old_price=None,
            available=True,
            stock=3,
            delivery_days=2,
            seller="Fixture Seller",
            adapter_schema_version="fake-v1",
            observed_at=datetime(2026, 7, 19, 10, 0, 2, tzinfo=UTC),
        )


class MixedAdapter(SuccessAdapter):
    async def fetch(self, request: AdapterRequest) -> NormalizedOffer:
        if request.external_id == "EXT-0":
            raise TimeoutError("fixture timeout")
        return await super().fetch(request)


class ConcurrencyAdapter(SuccessAdapter):
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def fetch(self, request: AdapterRequest) -> NormalizedOffer:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            return await super().fetch(request)
        finally:
            self.active -= 1


def test_scheduler_tick_processes_due_targets_and_clears_leases(db_session: Session) -> None:
    targets = _seed_targets(db_session, count=2)
    factory = _session_factory(db_session)
    registry = AdapterRegistry({"fake-scheduler": SuccessAdapter()})
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)

    results = asyncio.run(
        run_scheduler_tick(
            worker_id="worker-a",
            registry=registry,
            session_factory=factory,
            batch_size=10,
            concurrency=2,
            now=now,
            now_factory=lambda: now,
        )
    )

    assert [result.status for result in results] == ["succeeded", "succeeded"]
    with factory() as session:
        refreshed = list(session.scalars(select(MonitorTarget).order_by(MonitorTarget.id)).all())
        states = list(session.scalars(select(SupplierOfferState).order_by(SupplierOfferState.id)).all())
        attempts = list(session.scalars(select(MonitorAttempt).order_by(MonitorAttempt.id)).all())
    assert len(states) == 2
    assert len(attempts) == 2
    assert all(item.outcome == AttemptOutcome.SUCCESS.value for item in attempts)
    assert all(target.lease_token is None for target in refreshed)
    assert all(target.next_check_at.replace(tzinfo=UTC) == now + timedelta(seconds=300) for target in refreshed)
    assert {result.target_id for result in results} == {target.id for target in targets}


def test_failure_of_one_target_does_not_cancel_other_targets(db_session: Session) -> None:
    _seed_targets(db_session, count=2)
    factory = _session_factory(db_session)
    registry = AdapterRegistry({"fake-scheduler": MixedAdapter()})
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)

    results = asyncio.run(
        run_scheduler_tick(
            worker_id="worker-a",
            registry=registry,
            session_factory=factory,
            concurrency=2,
            now=now,
            now_factory=lambda: now,
        )
    )

    assert {result.status for result in results} == {"failed", "succeeded"}
    failed = next(result for result in results if result.status == "failed")
    assert failed.outcome is AttemptOutcome.TIMEOUT
    with factory() as session:
        outcomes = set(session.scalars(select(MonitorAttempt.outcome)).all())
        failure_counts = list(session.scalars(select(MonitorTarget.consecutive_failures)).all())
    assert outcomes == {AttemptOutcome.TIMEOUT.value, AttemptOutcome.SUCCESS.value}
    assert sorted(failure_counts) == [0, 1]


def test_scheduler_respects_network_concurrency_limit(db_session: Session) -> None:
    _seed_targets(db_session, count=5)
    factory = _session_factory(db_session)
    adapter = ConcurrencyAdapter()
    registry = AdapterRegistry({"fake-scheduler": adapter})
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)

    results = asyncio.run(
        run_scheduler_tick(
            worker_id="worker-a",
            registry=registry,
            session_factory=factory,
            batch_size=5,
            concurrency=2,
            now=now,
            now_factory=lambda: now,
        )
    )

    assert len(results) == 5
    assert adapter.max_active == 2


def test_missing_adapter_records_failure_and_backoff(db_session: Session) -> None:
    targets = _seed_targets(db_session, count=1, supplier_code="missing-adapter")
    factory = _session_factory(db_session)
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)

    results = asyncio.run(
        run_scheduler_tick(
            worker_id="worker-a",
            registry=AdapterRegistry(),
            session_factory=factory,
            now=now,
            now_factory=lambda: now,
        )
    )

    assert len(results) == 1
    assert results[0].status == "failed"
    assert results[0].outcome is AttemptOutcome.INTERNAL_ERROR
    with factory() as session:
        target = session.get(MonitorTarget, targets[0].id)
        attempt = session.scalar(select(MonitorAttempt))
    assert target is not None
    assert target.consecutive_failures == 1
    assert target.next_check_at.replace(tzinfo=UTC) == now + timedelta(seconds=600)
    assert attempt is not None
    assert attempt.error_code == "adapter_not_registered"


def test_scheduler_tick_returns_empty_when_nothing_is_due(db_session: Session) -> None:
    targets = _seed_targets(db_session, count=1)
    targets[0].next_check_at = datetime(2026, 7, 19, 10, 10, tzinfo=UTC)
    db_session.commit()
    factory = _session_factory(db_session)

    results = asyncio.run(
        run_scheduler_tick(
            worker_id="worker-a",
            registry=AdapterRegistry({"fake-scheduler": SuccessAdapter()}),
            session_factory=factory,
            now=datetime(2026, 7, 19, 10, 0, tzinfo=UTC),
        )
    )

    assert results == []
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(MonitorAttempt)) == 0
