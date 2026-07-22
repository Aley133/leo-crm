from backend.app.kaspi_product_enrichment_jobs import normalize_entry


def test_merchant_product_name_has_priority_over_non_product_names() -> None:
    entry = {
        "id": "entry-1",
        "attributes": {"quantity": 1},
        "customer": {"attributes": {"name": "Имя покупательницы"}},
    }
    product = {
        "id": "master-1",
        "attributes": {"name": "Название товара из master product"},
    }
    merchant_product = {
        "id": "merchant-1",
        "attributes": {
            "name": "SOLAB Берберин капсулы 60 шт",
            "code": "SKU-100",
        },
    }

    result = normalize_entry(
        entry,
        product=product,
        merchant_product=merchant_product,
    )

    assert result["name"] == "SOLAB Берберин капсулы 60 шт"
    assert result["sku"] == "SKU-100"
    assert result["external_product_id"] == "master-1"


def test_product_name_is_used_when_merchant_product_is_missing() -> None:
    entry = {"id": "entry-2", "attributes": {"title": "Entry fallback"}}
    product = {"id": "master-2", "attributes": {"name": "Точное название товара"}}

    result = normalize_entry(entry, product=product)

    assert result["name"] == "Точное название товара"
    assert result["external_product_id"] == "master-2"
