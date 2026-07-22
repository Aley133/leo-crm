from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_active_kaspi_orders_are_continuously_dispatched_to_browser_agent() -> None:
    dispatcher = (ROOT / "backend" / "app" / "kaspi_order_dispatch.py").read_text(encoding="utf-8")
    main = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")

    assert "MarketplaceOrder" in dispatcher
    assert "MarketplaceAccount.provider == \"kaspi\"" in dispatcher
    assert "encode_kaspi_seller_order_job" in dispatcher
    assert "latest_snapshot_at" in dispatcher
    assert "BrowserAgentJob.status.in_(_ACTIVE_JOB_STATUSES)" in dispatcher
    assert "dispatch_stale_kaspi_orders" in main
    assert "KASPI_ORDER_DISPATCH_INTERVAL_SECONDS = 60" in main
    assert "KASPI_ORDER_SNAPSHOT_REFRESH_SECONDS = 180" in main


def test_terminal_orders_are_not_polled_forever() -> None:
    dispatcher = (ROOT / "backend" / "app" / "kaspi_order_dispatch.py").read_text(encoding="utf-8")

    assert '"delivered"' in dispatcher
    assert '"cancelled"' in dispatcher
    assert '"returned"' in dispatcher
    assert "not_in(_TERMINAL_ORDER_STATUSES)" in dispatcher
