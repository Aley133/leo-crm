from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.models import Product
from backend.app.monitoring import AttemptOutcome, MonitorAttempt, MonitorStatus, MonitorTarget
from backend.app.scheduler_engine import AdapterRegistry, run_scheduler_tick
from backend.app.supplier_adapters.base import AdapterRequest
from backend.app.supplier_adapters.errors import AdapterCaptchaError
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct


def _session_factory(session: Session):
    return sessionmaker(bind=session.get_bind(), expire_on_commit=False)


def _seed_target(session: Session) -> MonitorTarget:
    supplier = Supplier(code="ozon", name="Ozon")
    product = Product(kaspi_product_id="CAPTCHA-1", merchant_sku="CAPTCHA-1", name="Captcha product")
    session.add_all([supplier, product])
    session.flush()

    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="123",
        title="Ozon product",
        url="https://www.ozon.ru/product/test-123/",
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


class CaptchaAdapter:
    code = "ozon-http-v1"
    access_strategy = "direct_http"

    async def fetch(self, request: AdapterRequest):
        raise AdapterCaptchaError("captcha fixture", http_status=403)


def test_scheduler_persists_typed_adapter_outcome_and_http_status(db_session: Session) -> None:
    target = _seed_target(db_session)
    factory = _session_factory(db_session)
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)

    results = asyncio.run(
        run_scheduler_tick(
            worker_id="worker-a",
            registry=AdapterRegistry({"ozon": CaptchaAdapter()}),
            session_factory=factory,
            now=now,
            now_factory=lambda: now,
        )
    )

    assert len(results) == 1
    assert results[0].status == "failed"
    assert results[0].outcome is AttemptOutcome.CAPTCHA

    with factory() as session:
        attempt = session.scalar(select(MonitorAttempt))
        refreshed = session.get(MonitorTarget, target.id)

    assert attempt is not None
    assert attempt.outcome == AttemptOutcome.CAPTCHA.value
    assert attempt.error_code == "adapter_captcha"
    assert attempt.http_status == 403
    assert refreshed is not None
    assert refreshed.consecutive_failures == 1
