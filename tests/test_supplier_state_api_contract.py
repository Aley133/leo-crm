from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_supplier_state_router_is_registered() -> None:
    main = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")

    assert "from .supplier_state_api import router as supplier_state_router" in main
    assert "app.include_router(supplier_state_router)" in main
    assert 'APP_VERSION = "0.14.0"' in main
    assert 'DEPLOYMENT_MARKER = "product-commerce-analytics-v1"' in main


def test_supplier_state_api_is_read_only_and_protected() -> None:
    source = (ROOT / "backend" / "app" / "supplier_state_api.py").read_text(encoding="utf-8")

    assert 'prefix="/api/supplier-state"' in source
    assert "dependencies=[Depends(require_service_token)]" in source
    assert '@router.get("/summary"' in source
    assert '@router.get("/offers"' in source
    assert "@router.post" not in source
    assert "@router.put" not in source
    assert "@router.patch" not in source
    assert "@router.delete" not in source


def test_supplier_state_summary_contract_contains_owner_metrics() -> None:
    source = (ROOT / "backend" / "app" / "supplier_state_api.py").read_text(encoding="utf-8")

    for field in (
        "total_products",
        "bound_products",
        "monitored_bindings",
        "offers_with_state",
        "available_offers",
        "unavailable_offers",
        "stale_offers",
        "degraded_targets",
        "failed_targets",
    ):
        assert field in source


def test_supplier_state_offer_contract_contains_control_plane_fields() -> None:
    source = (ROOT / "backend" / "app" / "supplier_state_api.py").read_text(encoding="utf-8")

    for field in (
        "kaspi_product_id",
        "product_name",
        "supplier_code",
        "supplier_product_url",
        "monitor_status",
        "consecutive_failures",
        "price",
        "currency",
        "available",
        "delivery_days",
        "last_checked_at",
        "is_stale",
    ):
        assert field in source

    for filter_name in (
        "supplier_code",
        "availability",
        "only_stale",
        "only_failures",
        "stale_after_minutes",
        "limit",
        "offset",
    ):
        assert filter_name in source
