from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_wildberries_purchase_controls_have_priority_over_out_of_stock_text() -> None:
    source = (
        ROOT / "backend" / "app" / "supplier_adapters" / "wildberries_browser_access.py"
    ).read_text(encoding="utf-8")

    function = source.split("async def _detect_stock", 1)[1].split(
        "async def _extract_visible_price", 1
    )[0]

    assert "visible enabled purchase control" in function
    assert "purchase text is visible" in function
    assert "visible product availability marker" in function
    assert function.index("purchase_selectors") < function.index("out_selectors")
    assert "if any(marker in low for marker in out_markers)" not in function


def test_wildberries_out_of_stock_is_a_successful_normalized_offer() -> None:
    source = (
        ROOT / "backend" / "app" / "supplier_adapters" / "wildberries_browser_access.py"
    ).read_text(encoding="utf-8")

    browser_flow = source.split("async def _fetch_browser_verified", 1)[1].split(
        "async def _body_text", 1
    )[0]

    assert 'if stock_status == "out_of_stock":' in browser_flow
    assert '"available": False' in browser_flow
    assert '"stock": 0' in browser_flow
    assert 'raise AdapterParseError("Wildberries product is out of stock")' not in browser_flow
