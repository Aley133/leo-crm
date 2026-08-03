from pathlib import Path

from backend.app.product_supplier_binding_api import _source_from_url


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


def test_manual_supplier_link_replacement_is_atomic_and_cancels_stale_jobs() -> None:
    source = (ROOT / "backend" / "app" / "product_supplier_binding_api.py").read_text(
        encoding="utf-8"
    )

    assert (
        '"/products/{product_id}/supplier-bindings/{binding_id}/manual"'
        in source
    )
    assert "def update_manual_supplier_binding(" in source
    assert "binding.supplier_product_id = supplier_product.id" in source
    assert 'job.error_code = "supplier_source_replaced"' in source
    assert "BrowserAgentJobStatus.LEASED.value" in source
    assert "job.lease_token = None" in source
    assert "queue_browser_target_now(" in source


def test_manual_supplier_binding_accepts_supported_marketplaces_and_ozon_kz() -> None:
    source = (ROOT / "backend" / "app" / "product_supplier_binding_api.py").read_text(encoding="utf-8")

    assert _source_from_url(
        "https://www.ozon.kz/product/arginin-51853964/?from=share"
    ) == ("ozon", "Ozon", "51853964")
    assert _source_from_url(
        "https://www.wildberries.ru/catalog/833814189/detail.aspx"
    ) == ("wb", "Wildberries", "833814189")
    assert "parse_supplier_url" in source
    assert "created_supplier_product" in source
    assert "created_binding" in source
    assert "queued_initial_check" in source
