from decimal import Decimal

from backend.app.monitoring import offer_fingerprint


def test_offer_fingerprint_is_stable_for_normalized_seller() -> None:
    first = offer_fingerprint(
        supplier_product_id=10,
        price=Decimal("4999.00"),
        available=True,
        stock=5,
        delivery_days=2,
        seller="  Ozon   Seller ",
        adapter_schema_version="ozon-v1",
    )
    second = offer_fingerprint(
        supplier_product_id=10,
        price=Decimal("4999.00"),
        available=True,
        stock=5,
        delivery_days=2,
        seller="ozon seller",
        adapter_schema_version="ozon-v1",
    )

    assert first == second


def test_offer_fingerprint_changes_when_business_fact_changes() -> None:
    first = offer_fingerprint(
        supplier_product_id=10,
        price=Decimal("4999.00"),
        available=True,
        stock=5,
        delivery_days=2,
        seller="Ozon Seller",
        adapter_schema_version="ozon-v1",
    )
    second = offer_fingerprint(
        supplier_product_id=10,
        price=Decimal("5199.00"),
        available=True,
        stock=5,
        delivery_days=2,
        seller="Ozon Seller",
        adapter_schema_version="ozon-v1",
    )

    assert first != second
