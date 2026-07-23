from types import SimpleNamespace

from backend.app.kaspi_product_enrichment_jobs import _match_line, normalize_entry


def test_merchant_product_is_authoritative_for_name_and_sku() -> None:
    entry = {
        "id": "entry-list-id",
        "attributes": {"name": "Unknown product"},
        "relationships": {"product": {"data": {"id": "master-1"}}},
    }
    product = {
        "id": "master-1",
        "attributes": {"name": "Fallback product", "code": "fallback-sku"},
    }
    merchant_product = {
        "id": "merchant-1",
        "attributes": {
            "name": "GLS Pharmaceuticals Магний цитрат 400 мг капсулы 180 шт",
            "code": "102656018_307802943",
        },
    }

    normalized = normalize_entry(
        entry,
        product=product,
        merchant_product=merchant_product,
    )

    assert normalized["name"] == "GLS Pharmaceuticals Магний цитрат 400 мг капсулы 180 шт"
    assert normalized["sku"] == "102656018_307802943"
    assert normalized["external_product_id"] == "master-1"


def test_single_line_order_survives_changed_temporary_entry_id() -> None:
    stored = SimpleNamespace(
        external_line_id="entry-from-list",
        merchant_sku=None,
        external_product_id="temporary-product-id",
    )
    normalized = {
        "entry_id": "entry-from-detail",
        "sku": "102656018_307802943",
        "external_product_id": "master-1",
    }

    assert _match_line([stored], normalized, normalized_count=1) is stored
