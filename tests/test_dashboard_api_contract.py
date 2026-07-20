from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_router_is_registered() -> None:
    main = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")

    assert "from .dashboard_api import router as dashboard_router" in main
    assert "app.include_router(dashboard_router)" in main


def test_dashboard_api_is_read_only_and_protected() -> None:
    source = (ROOT / "backend" / "app" / "dashboard_api.py").read_text(encoding="utf-8")

    assert 'prefix="/api/dashboard"' in source
    assert "dependencies=[Depends(require_service_token)]" in source
    assert '@router.get(""' in source
    assert "@router.post" not in source
    assert "@router.put" not in source
    assert "@router.patch" not in source
    assert "@router.delete" not in source


def test_dashboard_summary_contract_contains_owner_metrics() -> None:
    source = (ROOT / "backend" / "app" / "dashboard_api.py").read_text(encoding="utf-8")

    for model_name in (
        "ProductMetrics",
        "MonitoringMetrics",
        "SupplierMetrics",
        "DashboardSummary",
    ):
        assert f"class {model_name}" in source

    for field in (
        "generated_at",
        "stale_after_minutes",
        "without_supplier",
        "active",
        "degraded",
        "errors",
        "stale",
        "queued_jobs",
        "leased_jobs",
        "sources",
        "offers",
        "offers_with_state",
        "available",
        "unavailable",
    ):
        assert field in source


def test_dashboard_counts_browser_runtime_queue_separately() -> None:
    source = (ROOT / "backend" / "app" / "dashboard_api.py").read_text(encoding="utf-8")

    assert "BrowserAgentJobStatus.QUEUED.value" in source
    assert "BrowserAgentJobStatus.LEASED.value" in source
    assert "total_products - bound_products" in source
