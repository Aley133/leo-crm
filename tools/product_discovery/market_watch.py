"""Read Kaspi market state without touching Ozon or any trading settings."""
import asyncio
from tools.kaspi_fast_dumping_scanner import inspect_kaspi_product


async def inspect_market_card(card):
    async def read():
        result = await inspect_kaspi_product(reference=card["reference"], city_id=card["city_id"],
            zone_id=card["zone_id"], max_pages=20, strict_offers=True)
        offers = result["offers"]
        return {"price": offers["best_offer_price_kzt"], "seller_count": offers["seller_count_scanned"],
            "sellers_complete": offers["complete"], "empty_confirmed": False, "kaspi_url": result.get("product_url")}
    first = await read()
    if first["seller_count"] == 0:
        await asyncio.sleep(2)
        second = await read()
        if second["seller_count"] != 0 or not second["sellers_complete"]:
            raise ValueError("Повторная проверка не подтвердила отсутствие продавцов")
        first["empty_confirmed"] = True
    return first
