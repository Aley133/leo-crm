from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from backend.app.supplier_adapters.wildberries_browser_access import WildberriesBrowserAccessAdapter


ROOT = Path(__file__).resolve().parents[1]


def test_wildberries_adapter_extracts_nm_id_from_supported_urls() -> None:
    assert WildberriesBrowserAccessAdapter._extract_nm_id(
        "https://www.wildberries.ru/catalog/235129985/detail.aspx"
    ) == 235129985
    assert WildberriesBrowserAccessAdapter._extract_nm_id(
        "https://www.wb.ru/product/235129985"
    ) == 235129985


def test_wildberries_adapter_normalizes_tgbad_api_product_shape() -> None:
    node = {
        "id": 235129985,
        "name": "Формула сна Экспресс",
        "brand": "Эвалар",
        "salePriceU": 283000,
        "priceU": 319000,
        "supplierName": "Wildberries",
        "time1": 48,
        "sizes": [
            {"stocks": [{"qty": 3}, {"qty": 2}]},
            {"stocks": [{"qty": 1}]},
        ],
    }

    offer = WildberriesBrowserAccessAdapter._offer_from_node(node)

    assert offer is not None
    assert offer["price"] == Decimal("2830")
    assert offer["old_price"] == Decimal("3190")
    assert offer["currency"] == "KZT"
    assert offer["stock"] == 6
    assert offer["available"] is True
    assert offer["seller"] == "Wildberries"
    assert offer["delivery_days"] == 2


def test_wildberries_adapter_normalizes_current_nested_price_shape() -> None:
    node = {
        "id": 51853964,
        "name": "Формула сна Экспресс",
        "brand": "Эвалар",
        "sizes": [
            {
                "price": {
                    "basic": 319000,
                    "product": 283000,
                    "total": 274500,
                },
                "stocks": [{"qty": 4}, {"qty": 3}],
            }
        ],
    }

    assert WildberriesBrowserAccessAdapter._looks_like_product(node) is True
    offer = WildberriesBrowserAccessAdapter._offer_from_node(node)

    assert offer is not None
    assert offer["price"] == Decimal("2745")
    assert offer["old_price"] == Decimal("3190")
    assert offer["stock"] == 7
    assert offer["available"] is True


def test_wildberries_adapter_ports_tgbad_card_api_without_leaking_monolith() -> None:
    source = (
        ROOT / "backend" / "app" / "supplier_adapters" / "wildberries_browser_access.py"
    ).read_text(encoding="utf-8")

    assert "https://card.wb.ru/cards/v2/detail" in source
    assert "https://card.wb.ru/cards/detail" in source
    assert '"123585444"' in source
    assert '"-1257786"' in source
    assert "search.wb.ru" not in source
    assert "salePriceU" in source
    assert "priceU" in source
    assert 'for key in ("total", "product", "basic", "final", "discounted", "price")' in source
    assert "wb_card_api" in source
    assert "fetch_html" not in source
    assert "calculate_our_price" not in source
    assert "preOrder" not in source
    assert "telegram" not in source.casefold()
    assert "xml" not in source.casefold()
