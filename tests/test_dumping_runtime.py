from __future__ import annotations

from datetime import timedelta

from backend.app.dumping_api import read_dumping_runtime
from backend.app.dumping_models import DumpingRun
from backend.app.lease_engine import utc_now
from backend.app.models import Product


def _product(db_session, *, kaspi_id: str, name: str) -> Product:
    product = Product(
        kaspi_product_id=kaspi_id,
        merchant_sku=f"SKU-{kaspi_id}",
        name=name,
        status="active",
    )
    db_session.add(product)
    db_session.flush()
    return product


def test_dumping_runtime_isolated_from_supplier_monitoring(db_session) -> None:
    now = utc_now()
    active_product = _product(
        db_session,
        kaspi_id="111222333",
        name="Товар в проверке Kaspi",
    )
    queued_product = _product(
        db_session,
        kaspi_id="444555666",
        name="Товар в очереди Kaspi",
    )
    active = DumpingRun(
        product_id=active_product.id,
        status="leased_local",
        published=False,
        explanation_json={
            "agent_type": "kaspi_competitor",
            "agent_id": "kaspi-notebook-w1",
            "stage": "local_scan",
            "leased_at": now.isoformat(),
            "lease_until": (now + timedelta(minutes=3)).isoformat(),
            "updated_at": now.isoformat(),
        },
    )
    queued = DumpingRun(
        product_id=queued_product.id,
        status="queued_local",
        published=False,
        explanation_json={"agent_type": "kaspi_competitor"},
    )
    db_session.add_all([active, queued])
    db_session.commit()

    snapshot = read_dumping_runtime(db_session)

    assert snapshot.queued_count == 1
    assert len(snapshot.active_runs) == 1
    row = snapshot.active_runs[0]
    assert row.job_id == active.id
    assert row.status == "processing"
    assert row.agent_id == "kaspi-notebook-w1"
    assert row.product_name == "Товар в проверке Kaspi"
    assert "безопасную цену" in row.detail
    assert snapshot.latest_run is not None
    assert snapshot.latest_run.job_id == queued.id
    assert snapshot.latest_run.status == "queued"


def test_dumping_runtime_marks_expired_kaspi_lease(db_session) -> None:
    now = utc_now()
    product = _product(
        db_session,
        kaspi_id="777888999",
        name="Зависшая проверка Kaspi",
    )
    run = DumpingRun(
        product_id=product.id,
        status="leased_local",
        published=False,
        explanation_json={
            "agent_id": "kaspi-notebook-w2",
            "leased_at": (now - timedelta(minutes=4)).isoformat(),
            "lease_until": (now - timedelta(seconds=1)).isoformat(),
            "updated_at": now.isoformat(),
        },
    )
    db_session.add(run)
    db_session.commit()

    snapshot = read_dumping_runtime(db_session)

    assert snapshot.active_runs[0].status == "lease_expired"
    assert "не подтвердил завершение" in snapshot.active_runs[0].detail
