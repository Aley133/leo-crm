from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_product_card_exposes_per_order_manufactured_workflow() -> None:
    page = (ROOT / "backend" / "app" / "static" / "product-detail.html").read_text(
        encoding="utf-8"
    )
    script = (
        ROOT / "backend" / "app" / "static" / "product-inventory.js"
    ).read_text(encoding="utf-8")

    assert '<option value="production">Производство</option>' in page
    assert "Партии склада и производства" in page
    assert "Активные заказы этой партии" in script
    assert "Изготовлено" in script
    assert "/manufacture" in script
    assert "order_line_fully_allocated" in script


def test_production_migration_resets_legacy_automatic_allocations() -> None:
    migration = (
        ROOT
        / "migrations"
        / "versions"
        / "20260729_0025_production_batch_workflow.py"
    ).read_text(encoding="utf-8")

    assert "batch_type = 'production'" in migration
    assert "DELETE FROM inventory_allocations" in migration
    assert "quantity_remaining = 0" in migration
