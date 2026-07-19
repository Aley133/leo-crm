from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

import backend.app.scheduler_engine as scheduler_engine
from backend.app.models import Product
from backend.app.monitoring import MonitorAttempt, MonitorStatus, MonitorTarget, SupplierOfferState
from backend.app.scheduler_engine import AdapterRegistry, run_scheduler_tick
from backend.app.supplier_adapters.base import AdapterRequest, NormalizedOffer
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct


def _session_factory(session: Session):
    return sessionmaker(bind=session.get_bind(), expire_on_commit=False)


def _seed_due_target(session: Session) -> MonitorTarget:
    supplier = Supplier(code="persistence-test", name="Persistence Test")
    product = Product(
        kaspi_product_id="PERSIST-1",
        merchant_sku="PERSIST-1",
        name="Persistence boundary product",
    )
    session.add_all([supplier, product])
    session.flush()

    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="PERSIST-EXT-1",
        title="Persistence supplier product",
        url="https://example.com/persist-1",
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
    session.commit()
    return target


class SuccessfulAdapter:
    code = "persistence-adapter-v1"
    access_strategy = "fixture"

    async def fetch(self, request: AdapterRequest) -> NormalizedOffer:
        return NormalizedOffer(
            supplier_product_id=request.supplier_product_id,
            price=Decimal("5000.00"),
            old_price=None,
            available=True,
            stock=4,
            delivery_days=2,
            seller="Fixture Seller",
            adapter_schema_version="persistence-v1",
            observed_at=datetime(2026, 7, 19, 10, 0, 1, tzinfo=UTC),
        )


def test_success_persistence_failure_is_not_recorded_as_supplier_failure(
    db_session: Session,
    monkeypatch,
) -> None:
    target = _seed_due_target(db_session)
    factory = _session_factory(db_session)
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)

    def fail_success_persistence(*args, **kwargs):
        raise RuntimeError("database unavailable")

    def reject_supplier_failure_path(*args, **kwargs):
        raise AssertionError("supplier failure persistence must not be called")

    monkeypatch.setattr(
        scheduler_engine,
        "persist_successful_observation",
        fail_success_persistence,
    )
    monkeypatch.setattr(
        scheduler_engine,
        "persist_failed_attempt",
        reject_supplier_failure_path,
    )

    results = asyncio.run(
        run_scheduler_tick(
            worker_id="worker-a",
            registry=AdapterRegistry({"persistence-test": SuccessfulAdapter()}),
            session_factory=factory,
            now=now,
            now_factory=lambda: now,
        )
    )

    assert len(results) == 1
    assert results[0].status == "persistence_error"
    assert results[0].outcome is None
    assert results[0].error == "database unavailable"

    with factory() as session:
        refreshed = session.get(MonitorTarget, target.id)
        assert refreshed is not None
        assert refreshed.consecutive_failures == 0
        assert refreshed.lease_token is not None
        assert session.scalar(select(func.count()).select_from(MonitorAttempt)) == 0
        assert session.scalar(select(func.count()).select_from(SupplierOfferState)) == 0
