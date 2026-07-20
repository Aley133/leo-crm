from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_crm_dashboard_route_and_static_assets_are_registered() -> None:
    main = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
    ui = (ROOT / "backend" / "app" / "ui.py").read_text(encoding="utf-8")

    assert 'app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")' in main
    assert "app.include_router(ui_router)" in main
    assert '"crm": "/crm"' in main
    assert '@router.get("/crm"' in ui
    assert 'FileResponse(STATIC_DIR / "dashboard.html")' in ui


def test_dashboard_page_uses_live_dashboard_api_and_bearer_auth() -> None:
    html = (ROOT / "backend" / "app" / "static" / "dashboard.html").read_text(encoding="utf-8")
    script = (ROOT / "backend" / "app" / "static" / "dashboard.js").read_text(encoding="utf-8")

    assert "LEO CRM" in html
    assert 'id="dashboard"' in html
    assert 'id="token-form"' in html
    assert 'fetch("/api/dashboard"' in script
    assert "Authorization: `Bearer ${token}`" in script
    assert 'localStorage.getItem(storageKey)' in script


def test_dashboard_frontend_contains_no_business_calculation_rules() -> None:
    script = (ROOT / "backend" / "app" / "static" / "dashboard.js").read_text(encoding="utf-8")
    normalized = script.casefold()

    for forbidden in (
        "markup",
        "margin_pct",
        "delivery_fee",
        "price_floor",
        "supplier_score",
        "decision_engine",
    ):
        assert forbidden not in normalized


def test_dashboard_has_owner_attention_and_runtime_sections() -> None:
    html = (ROOT / "backend" / "app" / "static" / "dashboard.html").read_text(encoding="utf-8")

    for element_id in (
        "products-total",
        "products-unbound",
        "monitoring-active",
        "monitoring-errors",
        "attention-failures",
        "attention-stale",
        "attention-unavailable",
        "queue-count",
        "leased-count",
        "offers-total",
        "offers-available",
    ):
        assert f'id="{element_id}"' in html
