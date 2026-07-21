from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_kaspi_order_by_code_diagnostics_is_registered_and_read_only() -> None:
    source = (ROOT / "backend" / "app" / "marketplace_api.py").read_text(
        encoding="utf-8"
    )

    assert '@router.get("/orders/{order_code}/diagnostics/raw")' in source
    assert "def inspect_raw_order_by_code(" in source
    assert "transport.fetch_orders(" in source
    assert '"source_payload": payload' in source
    assert '"source_relationships": payload.get("relationships") or {}' in source
    assert '"leo_normalized_status": normalized.status' in source

    function_source = source.split("def inspect_raw_order_by_code(", 1)[1].split(
        '@router.post("/orders/sync-page")', 1
    )[0]
    assert "sync_kaspi_order_page" not in function_source
    assert "sync_kaspi_orders" not in function_source
    assert "MarketplaceImportCheckpoint" not in function_source
    assert "session.delete" not in function_source


def test_kaspi_order_by_code_diagnostics_has_bounded_scan() -> None:
    source = (ROOT / "backend" / "app" / "marketplace_api.py").read_text(
        encoding="utf-8"
    )
    function_source = source.split("def inspect_raw_order_by_code(", 1)[1].split(
        '@router.post("/orders/sync-page")', 1
    )[0]

    assert "page_size: int = Query(default=50, ge=1, le=100)" in function_source
    assert "max_pages: int = Query(default=20, ge=1, le=100)" in function_source
    assert "pages_scanned < max_pages" in function_source
    assert "Kaspi order was not found in the live API lookback window" in function_source
