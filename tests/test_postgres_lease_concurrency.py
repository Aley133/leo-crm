from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from backend.app import models, monitoring, suppliers  # noqa: F401
from backend.app.db import Base
from backend.app.lease_engine import claim_due_targets
from backend.app.models import Product
from backend.app.monitoring import MonitorTarget
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct


POSTGRES_TEST_URL = os.getenv("POSTGRES_TEST_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="POSTGRES_TEST_URL is required for real PostgreSQL concurrency tests",
)


def _seed(session: Session, now: datetime) -> int:
    product = Product(kaspi_product_id="PG-LEASE-1", merchant_sku="PG-LEASE-1", name="PG lease")
    supplier = Supplier(code="pg-lease", name="PG lease supplier")
    session.add_all([product, supplier])
    session.flush()
    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="PG-LEASE-1",
        title="PG lease product",
        url="https://example.com/pg-lease",
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
        next_check_at=now - timedelta(minutes=1),
    )
    session.add(target)
    session.commit()
    return target.id


def test_two_real_postgres_connections_never_claim_same_target() -> None:
    assert POSTGRES_TEST_URL is not None
    engine = create_engine(POSTGRES_TEST_URL, pool_size=4, max_overflow=0)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    Base.metadata.create_all(engine)

    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    with factory() as session:
        target_id = _seed(session, now)

    barrier = threading.Barrier(2)

    def worker(worker_id: str) -> list[int]:
        with factory() as session:
            barrier.wait(timeout=10)
            claims = claim_due_targets(
                session,
                lease_owner=worker_id,
                limit=1,
                lease_seconds=120,
                now=now,
            )
            return [claim.target_id for claim in claims]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ["worker-a", "worker-b"]))

    flattened = [target for result in results for target in result]
    assert flattened == [target_id]
    assert sorted(len(result) for result in results) == [0, 1]

    Base.metadata.drop_all(engine)
    engine.dispose()
