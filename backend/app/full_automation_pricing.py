"""Profit-first, delivery-aware repricing using a complete fresh seller field."""

from decimal import Decimal, ROUND_FLOOR
from .fast_dumping_pricing import (
    FastPriceDecision,
    _optional_money,
    LOGISTICS_PRICE_BREAKS,
)
from .commerce.profit_calculator import (
    kaspi_logistics_per_unit,
    KASPI_COMMISSION_RATE,
    TAX_RATE,
)


def decide_automated_price(
    *,
    own_price_kzt,
    safe_floor_kzt,
    market_offers,
    unit_cost_kzt,
    config=None,
    offers_complete=False,
    target_position=3,
):
    config = config or {}
    own = _optional_money(own_price_kzt)
    floor = Decimal(safe_floor_kzt)

    def result(target, status, reason, allowed=False):
        return FastPriceDecision(
            floor, None, own, target, Decimal(1), status, reason, allowed
        )

    if own is None or not offers_complete:
        return result(
            own,
            "automation_market_incomplete",
            "Нужны наша строка и полный рынок: обновите Fast Agent до 1.2.8.",
        )
    rivals = sorted(
        (
            r
            for r in market_offers
            if not r.get("is_own") and _optional_money(r.get("price_kzt"))
        ),
        key=lambda r: Decimal(str(r["price_kzt"])),
    )
    external = [r for r in rivals if not r.get("is_owned_group")]
    if not external:
        return result(
            own,
            "no_competitor",
            "Внешних продавцов нет; повышение без рыночного ориентира не выполняется.",
        )
    own_rows = [r for r in market_offers if r.get("is_own")]
    own_days = own_rows[0].get("delivery_days") if own_rows else None
    # Missing delivery data gives no premium. Only a configured, significant
    # advantage permits a higher price; it does not guarantee conversion.
    premium_day = Decimal(str(config.get("premium_per_day_kzt", 500)))
    premium_cap = Decimal(str(config.get("premium_cap_kzt", 2000)))
    ceilings = []
    for rival in external:
        days = rival.get("delivery_days")
        gap = (
            max(0, int(days) - int(own_days))
            if days is not None and own_days is not None
            else 0
        )
        premium = (
            min(premium_cap, premium_day * gap)
            if gap >= int(config.get("delivery_advantage_days", 4))
            else Decimal(0)
        )
        ceilings.append(
            Decimal(str(rival["price_kzt"]))
            + premium
            - (Decimal(0) if premium else Decimal(1))
        )
    ceiling = min(ceilings)
    rank = max(1, min(3, int(target_position)))
    if len(rivals) >= rank:
        ceiling = min(ceiling, Decimal(str(rivals[rank - 1]["price_kzt"])) - 1)
    if config.get("maximum_price_kzt"):
        ceiling = min(ceiling, Decimal(str(config["maximum_price_kzt"])))
    ceiling = ceiling.to_integral_value(rounding=ROUND_FLOOR)
    if ceiling < floor:
        return result(
            max(own, floor),
            "floor_limited",
            "Первая тройка недоступна при заданной минимальной прибыли; порог сохранён.",
            own < floor,
        )
    candidates = [ceiling] + [
        b - 1 for b in LOGISTICS_PRICE_BREAKS if floor <= b - 1 <= ceiling
    ]

    def profit(price):
        return (
            price * (1 - KASPI_COMMISSION_RATE - TAX_RATE)
            - kaspi_logistics_per_unit(price)
            - Decimal(unit_cost_kzt)
        )

    target = max(candidates, key=lambda p: (profit(p), p))
    peer_prices = [
        Decimal(str(r["price_kzt"])) for r in rivals if r.get("is_owned_group")
    ]
    peer_rank_cap = (
        len(rivals) >= rank
        and rivals[rank - 1].get("is_owned_group")
        and ceiling == Decimal(str(rivals[rank - 1]["price_kzt"])) - 1
    )
    if peer_prices and target < own and (target < min(peer_prices) or peer_rank_cap):
        return result(
            max(own, floor),
            "owned_peer_guard",
            "Снижение создало бы гонку цен между собственными магазинами; цена сохранена.",
            own < floor,
        )
    estimated = 1 + sum(Decimal(str(r["price_kzt"])) <= target for r in rivals)
    reason = (
        f"Максимальная расчётная прибыль {profit(target):.2f} ₸; место по цене ≈{estimated}, "
        f"цель TOP-{rank}. Надбавка за доставку ограничена {premium_cap} ₸. "
        "Позиция зависит также от сортировки Kaspi и адреса покупателя."
    )
    return result(
        target,
        "automation_ready" if target != own else "watching",
        reason,
        target != own,
    )
