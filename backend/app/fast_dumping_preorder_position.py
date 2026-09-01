from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


MONEY = Decimal("0.01")
PERCENT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class PreorderPositionDecision:
    target_price_kzt: Decimal
    desired_position: int
    estimated_position: int
    exact: bool
    anchor_price_kzt: Decimal | None
    external_sellers: int
    reason: str


def _money(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(MONEY)


def _offer_price(raw: object) -> Decimal | None:
    if raw in (None, "") or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw)).quantize(MONEY)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not value.is_finite() or value <= 0:
        return None
    return value


def _external_prices(
    market_offers: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    *,
    page_visible_price_kzt: Decimal | None = None,
) -> list[Decimal]:
    page_floor = None if page_visible_price_kzt is None else _money(page_visible_price_kzt)
    prices: list[Decimal] = []
    for raw in market_offers or []:
        if (
            not isinstance(raw, dict)
            or bool(raw.get("is_own"))
            or bool(raw.get("is_owned_group"))
        ):
            continue
        price = _offer_price(raw.get("price_kzt"))
        if price is None:
            continue
        # The scanner can retain diagnostics from another buyer/zone context.
        # As in the delivery TOP-5 guard, do not let prices below the public
        # headline distort the position strategy.
        if page_floor is not None and price < page_floor:
            continue
        prices.append(price)
    prices.sort()
    return prices


def decide_preorder_position(
    *,
    own_price_kzt: Decimal | None,
    safe_floor_kzt: Decimal,
    target_position: int,
    undercut_step_kzt: Decimal,
    allow_price_raise: bool,
    max_undercut_gap_percent: Decimal,
    market_offers: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    page_visible_price_kzt: Decimal | None = None,
) -> PreorderPositionDecision:
    """Choose the highest safe price that targets a requested price position.

    The strategy is intentionally supplier-preorder-only. A requested position
    P means P-1 external sellers should be cheaper than us. When market ties,
    hard floor, no-raise policy, anomaly protection or an insufficient seller
    count make the exact slot impossible, the closest safe result is returned
    and `exact` is false.
    """

    floor = _money(safe_floor_kzt)
    step = _money(undercut_step_kzt)
    desired = int(target_position)
    max_gap = Decimal(str(max_undercut_gap_percent)).quantize(PERCENT)
    if floor <= 0:
        raise ValueError("safe_floor_kzt must be greater than zero")
    if step <= 0:
        raise ValueError("undercut_step_kzt must be greater than zero")
    if desired < 1 or desired > 50:
        raise ValueError("target_position must be in 1..50")
    if max_gap <= 0 or max_gap > 100:
        raise ValueError("max_undercut_gap_percent must be in (0, 100]")

    own = None if own_price_kzt is None else _money(own_price_kzt)
    prices = _external_prices(
        market_offers,
        page_visible_price_kzt=page_visible_price_kzt,
    )
    n = len(prices)
    anchor: Decimal | None = None
    exact_candidate = True
    notes: list[str] = []

    if not prices:
        raw_target = own or floor
        exact_candidate = desired == 1
        notes.append("Внешних продавцов нет; сохраняем безопасную текущую цену.")
    elif desired == 1:
        anchor = prices[0]
        raw_target = max(Decimal("0.01"), anchor - step)
    elif desired <= n:
        previous = prices[desired - 2]
        anchor = prices[desired - 1]
        candidate = anchor - step
        if candidate > previous:
            raw_target = candidate
        else:
            # There is no price interval wide enough to guarantee the exact
            # slot. A tie at the upper boundary is the closest margin-friendly
            # price; Kaspi may order equal-price sellers by secondary factors.
            raw_target = anchor
            exact_candidate = False
            notes.append(
                "Между соседними продавцами нет ценового шага для гарантированного места; возможна ничья по цене."
            )
    else:
        anchor = prices[-1]
        raw_target = anchor + step
        if desired != n + 1:
            exact_candidate = False
            notes.append(
                f"На рынке только {n} внешних продавцов; ниже места №{n + 1} по цене опуститься невозможно."
            )

    target = max(_money(raw_target), floor)
    if target != _money(raw_target):
        exact_candidate = False
        notes.append(
            f"Безопасный floor {format(floor, 'f')} ₸ не позволяет поставить расчётную цену ниже."
        )

    if own is not None and target > own and not allow_price_raise:
        target = own
        exact_candidate = False
        notes.append("Повышение цены запрещено настройкой; оставлена текущая цена.")

    if own is not None and own > 0 and target < own:
        gap = ((own - target) / own * Decimal("100")).quantize(PERCENT)
        if gap > max_gap:
            target = own
            exact_candidate = False
            notes.append(
                f"Снижение до места №{desired} превышает защиту аномалии {format(max_gap, 'f')}%; цена не снижена."
            )

    target = _money(target)
    cheaper = sum(1 for price in prices if price < target)
    tied = sum(1 for price in prices if price == target)
    estimated = cheaper + 1
    exact = exact_candidate and estimated == desired and tied == 0
    if tied:
        exact = False
        notes.append(
            f"С ценой {format(target, 'f')} ₸ совпадают ещё {tied} продавца(ов); точный порядок зависит от Kaspi."
        )

    if exact:
        summary = (
            f"Стратегия «Место»: целим позицию №{desired}; расчётная позиция №{estimated}, "
            f"цена {format(target, 'f')} ₸."
        )
    else:
        summary = (
            f"Стратегия «Место»: запрошена позиция №{desired}; безопасный best-effort №{estimated}, "
            f"цена {format(target, 'f')} ₸."
        )
    if notes:
        summary += " " + " ".join(notes)

    return PreorderPositionDecision(
        target_price_kzt=target,
        desired_position=desired,
        estimated_position=estimated,
        exact=exact,
        anchor_price_kzt=anchor,
        external_sellers=n,
        reason=summary,
    )
