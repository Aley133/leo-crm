from decimal import Decimal

from backend.app.dumping_api import list_dumping_products
from backend.app.dumping_models import DumpingPolicy
from backend.app.models import Product


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
