from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_manual_supplier_binding_command_is_registered_and_atomic() -> None:
    main = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    source = (ROOT / "backend" / "app" / "product_supplier_binding_api.py").read_text(encoding="utf-8")

    assert "from .product_supplier_binding_api import router as product_supplier_binding_router" in main
    assert "app.include_router(product_supplier_binding_router)" in main
    assert 'prefix="/api/product-registry"' in source
    assert 'products/{product_id}/supplier-bindings/manual' in source
    assert "dependencies=[Depends(require_service_token)]" in source

    for model in (
        "Supplier",
        "SupplierProduct",
        "ProductBinding",
        "MonitorTarget",
        "BrowserAgentJob",
    ):
        assert model in source

    assert "BindingStatus.ACTIVE.value" in source
    assert "MonitorStatus.ACTIVE.value" in source
    assert "BrowserAgentJobStatus.QUEUED.value" in source
    assert "db.commit()" in source


def test_manual_supplier_binding_accepts_only_supported_marketplaces() -> None:
    source = (ROOT / "backend" / "app" / "product_supplier_binding_api.py").read_text(encoding="utf-8")

    assert 'return "ozon", "Ozon"' in source
    assert 'return "wb", "Wildberries"' in source
    assert "Поддерживаются только ссылки Ozon и Wildberries" in source
    assert "created_supplier_product" in source
    assert "created_binding" in source
    assert "queued_initial_check" in source
