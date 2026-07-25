from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_orders_page_uses_only_inline_purchase_transition_handler() -> None:
    html = (ROOT / "backend" / "app" / "static" / "orders.html").read_text(encoding="utf-8")
    script = (ROOT / "backend" / "app" / "static" / "orders.js").read_text(encoding="utf-8")

    assert 'src="/static/orders.js?' in html
    assert "orders-loading.js" not in html
    assert "window.location.reload()" not in script
    assert "await refreshSingleOrder(orderId, card)" in script
    assert 'type="button"' in script
