from backend.app.kaspi_product_enrichment_jobs import normalize_entry


def test_flat_merchant_product_restores_title_and_sku() -> None:
    entry = {
        "id": "entry-1",
        "attributes": {"quantity": 1, "basePrice": 8040},
    }
    product = {"id": "product-1", "productName": "Fallback product"}
    merchant = {
        "productName": "Полное название товара",
        "merchantSku": "996801988_SKU",
    }

    normalized = normalize_entry(entry, product=product, merchant_product=merchant)

    assert normalized["name"] == "Полное название товара"
    assert normalized["sku"] == "996801988_SKU"
    assert normalized["external_product_id"] == "product-1"
