from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.models import Product
from backend.app.monitoring import AttemptOutcome, MonitorAttempt, MonitorStatus, MonitorTarget, SourceHealth
from backend.app.scheduler_engine import AdapterRegistry, run_scheduler_tick
from backend.app.source_health_engine import apply_source_failure
from backend.app.supplier_adapters.base import AccessStrategy, AdapterRequest, NormalizedOffer
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct


def _session_factory(session: Session):
    return sessionmaker(bind=session.get_bind(), expire_on_commit=False)


def _seed_target(session: Session, *, now: datetime) -> tuple[MonitorTarget, Supplier]:
    supplier = Supplier(code="breaker-test", name="Breaker Test")
    product = Product(
        kaspi_product_id="BREAKER-1",
        merchant_sku="BREAKER-1",
        name="Breaker product",
    )
    session.add_all([supplier, product])
    session.flush()
    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="BREAKER-EXT-1",
        title="Breaker supplier product",
        url="https://example.com/breaker",
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
        next_check_at=now - timedelta(minutes=1),
    )
    session.add(target)
    session.commit()
    return target, supplier


class CountingAdapter:
    code = "breaker-adapter-v1"
    access_strategy = AccessStrategy.DIRECT_HTTP

    def __init__(self) -> None:
        self.calls = 0

    async def fetch(self, request: AdapterRequest) -> NormalizedOffer:
        self.calls += 1
        raise AssertionError("blocked source must not reach adapter.fetch")


def test_open_breaker_skips_adapter_and_defers_target(db_session: Session) -> None:
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    target, supplier = _seed_target(db_session, now=now)
    health = apply_source_failure(
        db_session,
        supplier_id=supplier.id,
        access_strategy=AccessStrategy.DIRECT_HTTP,
        outcome=AttemptOutcome.BLOCKED,
        error_code="http_403_blocked",
        occurred_at=now,
    )
    db_session.commit()
    blocked_until = health.blocked_until
    assert blocked_until is not None

    adapter = CountingAdapter()
    factory = _session_factory(db_session)
    results = asyncio.run(
        run_scheduler_tick(
            worker_id="worker-a",
            registry=AdapterRegistry({"breaker-test": adapter}),
            session_factory=factory,
            now=now,
            now_factory=lambda: now,
        )
    )

    assert len(results) == 1
    assert results[0].status == "source_blocked"
    assert adapter.calls == 0
    with factory() as session:
        refreshed = session.get(MonitorTarget, target.id)
        assert refreshed is not None
        assert refreshed.lease_token is None
        assert refreshed.lease_owner is None
        assert refreshed.next_check_at.replace(tzinfo=UTC) == blocked_until.replace(tzinfo=UTC)
        assert session.scalar(select(func.count()).select_from(MonitorAttempt)) == 0
        assert session.scalar(select(func.count()).select_from(SourceHealth)) == 1
