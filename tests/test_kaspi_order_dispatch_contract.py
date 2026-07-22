from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_active_kaspi_orders_are_dispatched_only_by_manual_rebuild() -> None:
    dispatcher = (ROOT / "backend" / "app" / "kaspi_order_dispatch.py").read_text(encoding="utf-8")
    commerce_api = (ROOT / "backend" / "app" / "commerce" / "api.py").read_text(encoding="utf-8")
    main = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")

    assert "dispatch_recent_kaspi_orders" in dispatcher
    assert "MarketplaceAccount.provider == \"kaspi\"" in dispatcher
    assert "encode_kaspi_seller_order_job" in dispatcher
    assert "BrowserAgentJob.status.in_(_ACTIVE_JOB_STATUSES)" in dispatcher
    assert '@router.post("/orders/rebuild")' in commerce_api
    assert "dispatch_recent_kaspi_orders" in commerce_api
    assert "dispatch_stale_kaspi_orders" not in main
    assert "KASPI_ORDER_DISPATCH_INTERVAL_SECONDS" not in main


def test_terminal_orders_are_not_queued_for_manual_rebuild() -> None:
    dispatcher = (ROOT / "backend" / "app" / "kaspi_order_dispatch.py").read_text(encoding="utf-8")

    assert '"delivered"' in dispatcher
    assert '"cancelled"' in dispatcher
    assert '"returned"' in dispatcher
    assert "not_in(_TERMINAL_ORDER_STATUSES)" in dispatcher
