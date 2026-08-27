from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .dumping_service import calculate_safe_floor


@dataclass(frozen=True, slots=True)
class InitialOfferPrice:
    price_kzt: Decimal
    safe_floor_kzt: Decimal
    competitor_price_kzt: Decimal | None
    status: str


def choose_initial_offer_price(
    *,
    supplier_cost_kzt: Decimal,
    minimum_profit_kzt: Decimal,
    competitor_price_kzt: Decimal | None,
    undercut_step_kzt: int,
) -> InitialOfferPrice:
    """Choose only the first Kaspi price; Fast Dumping owns every later move."""

    floor = calculate_safe_floor(
        unit_cost_kzt=Decimal(supplier_cost_kzt),
        minimum_profit_kzt=Decimal(minimum_profit_kzt),
    )
    competitor = None if competitor_price_kzt is None else Decimal(competitor_price_kzt)
    if competitor is None or competitor <= 0:
        return InitialOfferPrice(floor, floor, None, "safe_floor_no_competitor")
    undercut = max(Decimal("1"), competitor - Decimal(max(1, int(undercut_step_kzt))))
    if floor <= undercut:
        return InitialOfferPrice(undercut, floor, competitor, "below_kaspi_competitor")
    return InitialOfferPrice(floor, floor, competitor, "safe_floor_above_market")
