from __future__ import annotations

from datetime import timedelta

from backend.app.browser_agent_models import BrowserAgentJob
from backend.app.dumping_models import DumpingRun
from backend.app.lease_engine import utc_now
from backend.app.models import Product
from backend.app.monitoring import MonitorTarget
from backend.app.monitoring_center_api import list_active_monitoring_runs
from backend.app.suppliers import ProductBinding, Supplier, SupplierProduct


def _seed_product_and_supplier_run(db_session):
    now = utc_now()
    product = Product(
        kaspi_product_id="123456789",
        merchant_sku="SKU-LEO-1",
        name="Тестовый товар",
        status="active",
    )
    supplier = Supplier(code="wb", name="Wildberries")
    db_session.add_all([product, supplier])
    db_session.flush()

    supplier_product = SupplierProduct(
        supplier_id=supplier.id,
        external_id="987654321",
        title="Тестовый товар у поставщика",
        url="https://www.wildberries.ru/catalog/987654321/detail.aspx",
    )
    db_session.add(supplier_product)
    db_session.flush()

    binding = ProductBinding(
        product_id=product.id,
        supplier_product_id=supplier_product.id,
        status="active",
    )
    db_session.add(binding)
    db_session.flush()

    target = MonitorTarget(
        product_binding_id=binding.id,
        status="active",
        interval_seconds=300,
        next_check_at=now,
    )
    db_session.add(target)
    db_session.flush()

    supplier_job = BrowserAgentJob(
        monitor_target_id=target.id,
        supplier_product_id=supplier_product.id,
        url=supplier_product.url,
        status="leased",
        lease_owner="leo-windows-PC-w1",
        lease_token="a" * 48,
        lease_until=now + timedelta(minutes=3),
    )
    kaspi_job = DumpingRun(
        product_id=product.id,
        status="leased_local",
        published=False,
        explanation_json={
            "agent_type": "kaspi_competitor",
            "agent_id": "kaspi-competitor-PC-w1",
            "leased_at": now.isoformat(),
            "lease_until": (now + timedelta(minutes=3)).isoformat(),
            "updated_at": now.isoformat(),
        },
    )
    db_session.add_all([supplier_job, kaspi_job])
    db_session.commit()
    return supplier_job, kaspi_job


def test_active_runs_unify_supplier_and_kaspi_agents(db_session) -> None:
    supplier_job, kaspi_job = _seed_product_and_supplier_run(db_session)

    rows = list_active_monitoring_runs(db_session)

    assert {row.run_key for row in rows} == {
        f"supplier:{supplier_job.id}",
        f"kaspi:{kaspi_job.id}",
    }
    supplier = next(row for row in rows if row.runtime == "supplier_monitoring")
    kaspi = next(row for row in rows if row.runtime == "kaspi_competitor")
    assert supplier.status == "processing"
    assert supplier.source_code == "wb"
    assert supplier.source_name == "Wildberries"
    assert supplier.merchant_sku == "SKU-LEO-1"
    assert supplier.agent_id == "leo-windows-PC-w1"
    assert kaspi.status == "processing"
    assert kaspi.source_code == "kaspi"
    assert kaspi.source_name == "Kaspi"
    assert kaspi.agent_id == "kaspi-competitor-PC-w1"


def test_active_runs_mark_expired_agent_lease_as_stalled(db_session) -> None:
    supplier_job, _ = _seed_product_and_supplier_run(db_session)
    supplier_job.lease_until = utc_now() - timedelta(seconds=1)
    db_session.commit()

    rows = list_active_monitoring_runs(db_session)

    supplier = next(row for row in rows if row.runtime == "supplier_monitoring")
    assert supplier.status == "lease_expired"
    assert "не подтвердил завершение" in supplier.detail
