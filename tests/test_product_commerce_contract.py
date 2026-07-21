from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_product_commerce_api_is_registered_and_read_only() -> None:
    main = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    api = (ROOT / "backend" / "app" / "product_commerce_api.py").read_text(encoding="utf-8")

    assert "product_commerce_router" in main
    assert 'app.include_router(product_commerce_router)' in main
    assert '@router.get("/{product_id}/commerce"' in api
    assert "ProductCommerceAnalyzer.analyze(" in api
    assert "MarketplaceOrderLine.product_id == product_id" in api
    assert "estimated_gross_profit_before_fees" in api
    assert "profit_is_estimated" in api
    assert "FIFO" in api
    assert "@router.post" not in api
    assert "@router.put" not in api
    assert "@router.patch" not in api


def test_commerce_domain_does_not_depend_on_database_or_marketplace_clients() -> None:
    source = (ROOT / "backend" / "app" / "product_commerce.py").read_text(encoding="utf-8")

    assert "sqlalchemy" not in source
    assert "fastapi" not in source
    assert "kaspi" not in source.lower()
    assert "ProductCommerceAnalyzer" in source
    assert 'mode="stock"' in source
    assert 'mode="trial_batch"' in source
    assert 'mode="preorder"' in source
