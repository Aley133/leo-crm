from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_kaspi_raw_page_diagnostics_is_registered_and_read_only() -> None:
    source = (ROOT / "backend" / "app" / "marketplace_api.py").read_text(encoding="utf-8")

    assert '@router.get("/orders/diagnostics/raw-page")' in source
    assert "transport.fetch_orders(" in source
    assert 'cursor=str(page_number)' in source
    assert 'updated_after=None' in source
    assert '"source_status"' in source
    assert '"source_state"' in source
    assert '"date_like_fields"' in source
    assert '"source_attributes"' in source
    assert '"leo_normalized_status"' in source

    diagnostic_block = source.split('@router.get("/orders/diagnostics/raw-page")', 1)[1].split(
        '@router.post("/orders/sync-page")', 1
    )[0]
    assert "sync_kaspi_order_page" not in diagnostic_block
    assert "sync_kaspi_orders" not in diagnostic_block
    assert "MarketplaceImportCheckpoint" not in diagnostic_block
