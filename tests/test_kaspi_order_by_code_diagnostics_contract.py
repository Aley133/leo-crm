from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_kaspi_order_by_code_diagnostics_is_registered_and_read_only() -> None:
    source = (ROOT / "backend" / "app" / "marketplace_api.py").read_text(
        encoding="utf-8"
    )

    assert '@router.get("/orders/{order_code}/diagnostics/raw")' in source
    assert "def inspect_raw_order_by_code(" in source
    assert "transport.fetch_order_by_code(normalized_code)" in source
    assert '"lookup": "filter[orders][code]"' in source
    assert '"source_payload": payload' in source
    assert '"source_relationships": payload.get("relationships") or {}' in source
    assert '"leo_normalized_status": normalized.status' in source

    function_source = source.split("def inspect_raw_order_by_code(", 1)[1].split(
        '@router.post("/orders/sync-page")', 1
    )[0]
    assert "transport.fetch_orders(" not in function_source
    assert "sync_kaspi_order_page" not in function_source
    assert "sync_kaspi_orders" not in function_source
    assert "MarketplaceImportCheckpoint" not in function_source
    assert "session.delete" not in function_source


def test_kaspi_order_by_code_diagnostics_does_not_use_live_lookback_scan() -> None:
    source = (ROOT / "backend" / "app" / "marketplace_api.py").read_text(
        encoding="utf-8"
    )
    function_source = source.split("def inspect_raw_order_by_code(", 1)[1].split(
        '@router.post("/orders/sync-page")', 1
    )[0]

    assert "page_size" not in function_source
    assert "max_pages" not in function_source
    assert "pages_scanned" not in function_source
    assert "live API lookback window" not in function_source
    assert "filter[orders][code]" in function_source
