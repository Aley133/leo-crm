from __future__ import annotations

import json

from tools.ozon_http.parser import parse_other_seller_offers


def test_other_seller_parser_reads_nested_delivery_with_internal_sku() -> None:
    payload = {
        "widgetStates": {
            "webSellerList-555555555-default-1": json.dumps(
                {
                    "sellers": [
                        {
                            "sku": "9275848611",
                            "name": "Ozon seller",
                            "price": {"price": "2 250 ₸"},
                            "productLink": "/product/solgar-magnesium-555555555/",
                            "delivery": {"label": "Доставим через 3 дня"},
                        }
                    ]
                },
                ensure_ascii=False,
            )
        }
    }

    parsed = parse_other_seller_offers(payload)

    assert parsed["offer_count"] == 1
    offer = parsed["offers"][0]
    assert offer["offer_sku"] == "9275848611"
    assert offer["product_url"] == "https://ozon.kz/product/solgar-magnesium-555555555/"
    assert offer["price_kzt"] == 2250
    assert offer["delivery_text"] == "Доставим через 3 дня"
    assert offer["delivery_days"] == 3
