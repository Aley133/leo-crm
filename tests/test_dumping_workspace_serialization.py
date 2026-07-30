from decimal import Decimal
from pathlib import Path

from backend.app.dumping_api import list_dumping_products
from backend.app.dumping_competitor_worker import state_for_product
from backend.app.dumping_models import DumpingRun
from backend.app.dumping_models import DumpingPolicy
from backend.app.models import Product


ROOT = Path(__file__).resolve().parents[1]


def test_dumping_workspace_is_json_safe_after_first_policy(db_session) -> None:
    product = Product(
        kaspi_product_id="114810742_608899861",
        merchant_sku="114810742_608899861",
        name="GLS Pharmaceuticals Ягоды Годжи капсулы 90 шт",
        status="active",
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        DumpingPolicy(
            product_id=product.id,
            enabled=True,
            minimum_profit_kzt=Decimal("1000"),
            undercut_step_kzt=1,
            supplier_delivery_buffer_days=1,
            inventory_first=True,
            auto_publish_xml=True,
            city_id="750000000",
            zone_id="Magnum_ZONE1",
        )
    )
    db_session.commit()

    rows = list_dumping_products(db_session)

    assert len(rows) == 1
    row = rows[0]
    assert row["product_id"] == product.id
    assert isinstance(row["policy"], dict)
    assert row["policy"]["minimum_profit_kzt"] == Decimal("1000")
    assert row["source"] is None
    assert row["source_error"] is None
    assert row["latest_run"] is None


def test_dumping_workspace_reuses_request_session_for_scan_state(
    db_session,
    monkeypatch,
) -> None:
    product = Product(
        kaspi_product_id="123456789",
        merchant_sku="SKU-123456789",
        name="Товар с состоянием проверки",
        status="active",
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        DumpingPolicy(
            product_id=product.id,
            enabled=True,
            minimum_profit_kzt=Decimal("1000"),
            undercut_step_kzt=1,
            supplier_delivery_buffer_days=1,
            inventory_first=True,
            auto_publish_xml=True,
            city_id="750000000",
            zone_id="Magnum_ZONE1",
        )
    )
    run = DumpingRun(
        product_id=product.id,
        status="queued_local",
        published=False,
        explanation_json={"reason": "manual"},
    )
    db_session.add(run)
    db_session.commit()

    def fail_if_new_session_is_opened():
        raise AssertionError("dumping workspace opened a nested database session")

    monkeypatch.setattr(
        "backend.app.dumping_competitor_worker.SessionLocal",
        fail_if_new_session_is_opened,
    )

    rows = list_dumping_products(db_session)

    assert rows[0]["scan_state"]["status"] == "queued"
    assert state_for_product(product.id, db=db_session)["job_id"] == run.id


def test_dumping_ui_prefers_current_floor_over_stale_run_floor() -> None:
    source = (ROOT / "backend" / "app" / "static" / "dumping.js").read_text(
        encoding="utf-8"
    )

    assert (
        "const safeFloor = preview.safe_floor_kzt ?? run.safe_floor_kzt;"
        in source
    )
