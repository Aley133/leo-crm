from __future__ import annotations

import os
import time
from threading import Event
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import sessionmaker

from backend.app.db import Base
from backend.app.lease_engine import claim_target, due_target_statement
from backend.app.models import Product
from backend.app.monitoring import (
    MonitorAttempt,
    MonitorStatus,
    MonitorTarget,
    SupplierOfferObservation,
    SupplierOfferState,
)
from backend.app.observation_engine import StaleLeaseError, persist_successful_observation
from backend.app.supplier_adapters.base import NormalizedOffer
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct

pytestmark = pytest.mark.postgres


def _database_url() -> str:
    value = os.getenv("POSTGRES_TEST_DATABASE_URL")
    if not value:
        pytest.skip("POSTGRES_TEST_DATABASE_URL is not configured")
    return value


@pytest.fixture()
def postgres_factory():
    engine = create_engine(_database_url(), pool_size=5, max_overflow=0)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _seed_target(factory, *, now: datetime) -> tuple[int, int]:
    with factory() as session:
        supplier = Supplier(code="pg-concurrency", name="PostgreSQL Concurrency")
        product = Product(
            kaspi_product_id="PG-CONCURRENCY-1",
            merchant_sku="PG-CONCURRENCY-1",
            name="PostgreSQL concurrency product",
        )
        session.add_all([supplier, product])
        session.flush()

        supplier_product = SupplierProduct(
            supplier_id=supplier.id,
            external_id="PG-EXT-1",
            title="PostgreSQL supplier product",
            url="https://example.com/postgres-concurrency",
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
        return target.id, supplier_product.id


def _offer(supplier_product_id: int, *, price: str, observed_at: datetime) -> NormalizedOffer:
    return NormalizedOffer(
        supplier_product_id=supplier_product_id,
        price=Decimal(price),
        old_price=None,
        available=True,
        stock=5,
        delivery_days=2,
        seller="PostgreSQL Fixture Seller",
        adapter_schema_version="pg-v1",
        observed_at=observed_at,
    )


def test_fast_due_batches_skip_locked_rows_without_duplicate_jobs(postgres_factory):
    from backend.app.fast_dumping_models import (
        FastDumpingJob, FastDumpingPolicy, FastDumpingState,
    )
    from backend.app.fast_dumping_service import schedule_due_scans

    with postgres_factory() as seed:
        for i in range(12):
            product = Product(
                name=f"Fast batch {i}", kaspi_product_id=f"PG-FAST-{i}",
                merchant_sku=f"PG-FAST-{i}", sale_enabled=True,
            )
            seed.add(product)
            seed.flush()
            policy = FastDumpingPolicy(product_id=product.id, enabled=True)
            seed.add(policy)
            seed.flush()
            seed.add(FastDumpingState(policy_id=policy.id, product_id=product.id))
        seed.commit()

    with postgres_factory() as first, postgres_factory() as second:
        second.execute(text("SET LOCAL statement_timeout = '2s'"))
        assert schedule_due_scans(
            first, workspace_id=1, limit=10, recover_inventory_transitions=False,
        ) == 10
        # The first transaction still owns its locks. The second must make
        # progress on the remaining rows, without waiting or duplicating work.
        assert schedule_due_scans(
            second, workspace_id=1, limit=10, recover_inventory_transitions=False,
        ) == 2
        second.commit()
        first.commit()

    with postgres_factory() as check:
        jobs = check.scalars(select(FastDumpingJob)).all()
        states = check.scalars(select(FastDumpingState)).all()
        assert len(jobs) == len(states) == 12
        assert len({job.product_id for job in jobs}) == 12
        job_ids = {job.product_id: job.id for job in jobs}
        assert all(state.active_job_id == job_ids[state.product_id] for state in states)
        assert schedule_due_scans(
            check, workspace_id=1, recover_inventory_transitions=False,
        ) == 0


def test_xml_import_waits_for_feed_before_updating_products(postgres_factory):
    from backend.app.dumping_models import KaspiXmlFeed
    from backend.app.fast_dumping_models import FastDumpingPolicy
    from backend.app.product_xml_import_api import _commit_xml_import

    xml = b"""<kaspi_catalog><offers><offer sku='LOCK-TEST'>
    <model>Before</model><price>6000</price>
    <availability available='yes' stockCount='5' preOrder='5'/>
    </offer></offers></kaspi_catalog>"""
    with postgres_factory() as seed:
        _commit_xml_import(xml, source_filename="initial.xml", db=seed)
        product = seed.scalar(select(Product).where(Product.kaspi_product_id == "LOCK-TEST"))
        product_id = product.id
        seed.add(FastDumpingPolicy(product_id=product_id))
        seed.commit()

    with postgres_factory() as mirror:
        mirror.execute(text("SET LOCAL lock_timeout = '2s'"))
        feed = mirror.scalar(select(KaspiXmlFeed).with_for_update())
        waiting_for_feed = Event()

        def import_archive():
            with postgres_factory() as importer:
                importer.execute(text("SET LOCAL lock_timeout = '5s'"))

                def before_sql(conn, cursor, statement, parameters, context, executemany):
                    if "kaspi_xml_feeds" in statement and "FOR UPDATE" in statement:
                        waiting_for_feed.set()

                event.listen(importer.connection(), "before_cursor_execute", before_sql)
                return _commit_xml_import(
                    xml.replace(b"Before", b"After").replace(b"6000", b"1"),
                    source_filename="archive.xml", db=importer,
                )

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(import_archive)
            try:
                assert waiting_for_feed.wait(timeout=3)
                assert not future.done()
                # A product-first importer would already hold this row while
                # waiting for our feed, creating the production deadlock.
                product = mirror.scalar(
                    select(Product).where(Product.id == product_id).with_for_update()
                )
                product.brand = "Concurrent update"
                feed.generated_xml = feed.generated_xml.replace("6000", "7000")
                mirror.commit()
            finally:
                mirror.rollback()
            assert future.result(timeout=5)["updated_count"] == 1

    with postgres_factory() as check:
        feed = check.scalar(select(KaspiXmlFeed))
        assert ">7000<" in feed.generated_xml
        assert ">1<" in feed.source_xml
        product = check.get(Product, product_id)
        assert product.name == "After"
        assert product.brand == "Concurrent update"


def test_skip_locked_returns_immediately_for_row_locked_by_another_session(postgres_factory) -> None:
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    target_id, _ = _seed_target(postgres_factory, now=now)

    first = postgres_factory()
    second = postgres_factory()
    try:
        locked = list(first.scalars(due_target_statement(now=now, limit=1)).all())
        assert [target.id for target in locked] == [target_id]

        started = time.monotonic()
        skipped = list(second.scalars(due_target_statement(now=now, limit=1)).all())
        elapsed = time.monotonic() - started

        assert skipped == []
        assert elapsed < 1.0
    finally:
        first.rollback()
        second.rollback()
        first.close()
        second.close()


def test_first_offer_state_creation_is_serialized_by_parent_lock(postgres_factory) -> None:
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    target_id, supplier_product_id = _seed_target(postgres_factory, now=now)

    with postgres_factory() as claim_session:
        result = claim_target(
            claim_session,
            target_id=target_id,
            lease_owner="worker-a",
            lease_seconds=120,
            now=now,
        )
    assert result.claim is not None
    claim = result.claim

    first = postgres_factory()
    first.execute(text("SET LOCAL lock_timeout = '5s'"))

    try:
        first_result = persist_successful_observation(
            first,
            monitor_target_id=target_id,
            lease_token=claim.lease_token,
            adapter_code="pg-adapter-v1",
            access_strategy="direct_http",
            started_at=now,
            finished_at=now + timedelta(seconds=1),
            offer=_offer(
                supplier_product_id,
                price="5000.00",
                observed_at=now + timedelta(seconds=1),
            ),
        )
        assert first_result.changed is True

        def persist_second():
            with postgres_factory() as second:
                second.execute(text("SET LOCAL lock_timeout = '5s'"))
                result = persist_successful_observation(
                    second,
                    monitor_target_id=target_id,
                    lease_token=claim.lease_token,
                    adapter_code="pg-adapter-v1",
                    access_strategy="direct_http",
                    started_at=now + timedelta(seconds=2),
                    finished_at=now + timedelta(seconds=3),
                    offer=_offer(
                        supplier_product_id,
                        price="5000.00",
                        observed_at=now + timedelta(seconds=3),
                    ),
                )
                second.commit()
                return result

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(persist_second)
            time.sleep(0.2)
            assert future.done() is False
            first.commit()
            second_result = future.result(timeout=5)

        assert second_result.changed is False

        with postgres_factory() as verification:
            assert verification.scalar(select(func.count()).select_from(SupplierOfferState)) == 1
            assert verification.scalar(select(func.count()).select_from(SupplierOfferObservation)) == 1
            assert verification.scalar(select(func.count()).select_from(MonitorAttempt)) == 2
    finally:
        first.rollback()
        first.close()


def test_reclaimed_lease_rejects_stale_observation_without_side_effects(postgres_factory) -> None:
    now = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)
    target_id, supplier_product_id = _seed_target(postgres_factory, now=now)

    with postgres_factory() as session:
        first_result = claim_target(
            session,
            target_id=target_id,
            lease_owner="worker-a",
            lease_seconds=1,
            now=now,
        )
    assert first_result.claim is not None
    stale_claim = first_result.claim

    with postgres_factory() as session:
        second_result = claim_target(
            session,
            target_id=target_id,
            lease_owner="worker-b",
            lease_seconds=120,
            now=now + timedelta(seconds=2),
        )
    assert second_result.claim is not None
    current_claim = second_result.claim

    with postgres_factory() as stale_session:
        with pytest.raises(StaleLeaseError):
            persist_successful_observation(
                stale_session,
                monitor_target_id=target_id,
                lease_token=stale_claim.lease_token,
                adapter_code="pg-adapter-v1",
                access_strategy="direct_http",
                started_at=now,
                finished_at=now + timedelta(seconds=3),
                offer=_offer(
                    supplier_product_id,
                    price="5000.00",
                    observed_at=now + timedelta(seconds=3),
                ),
            )
        stale_session.rollback()

    with postgres_factory() as verification:
        target = verification.get(MonitorTarget, target_id)
        assert target is not None
        assert target.lease_owner == "worker-b"
        assert target.lease_token == current_claim.lease_token
        assert verification.scalar(select(func.count()).select_from(MonitorAttempt)) == 0
        assert verification.scalar(select(func.count()).select_from(SupplierOfferState)) == 0
        assert verification.scalar(select(func.count()).select_from(SupplierOfferObservation)) == 0
