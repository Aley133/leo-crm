from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any


MONEY = Decimal("0.01")
PERCENT = Decimal("0.01")
DEFAULT_VISIBLE_SELLERS = 5


@dataclass(frozen=True, slots=True)
class FastPriceDecision:
    safe_floor_kzt: Decimal
    competitor_price_kzt: Decimal | None
    own_price_kzt: Decimal | None
    target_price_kzt: Decimal | None
    undercut_step_kzt: Decimal
    status: str
    reason: str
    write_allowed: bool
    gap_percent: Decimal | None = None
    max_undercut_gap_percent: Decimal | None = None


@dataclass(frozen=True, slots=True)
class DeliveryBusinessPlan:
    """Commercial ceiling created by the faster-delivery advantage.

    `effective_competitor_price_kzt` is a synthetic anchor consumed by the
    ordinary repricing formula. The resulting target is still `anchor - step`,
    so floor, anomaly and no-raise protections remain centralized in one place.
    """

    effective_competitor_price_kzt: Decimal
    target_ceiling_kzt: Decimal
    premium_ceiling_kzt: Decimal
    top5_ceiling_kzt: Decimal | None
    selected_competitor_ceiling_kzt: Decimal | None
    ignored_count: int
    ignored_names: tuple[str, ...]
    reason: str


def _money(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(MONEY)


def _percent(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(PERCENT)


def _optional_money(value: object) -> Decimal | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value)).quantize(MONEY)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not result.is_finite() or result <= 0:
        return None
    return result


def _optional_decimal(value: object) -> Decimal | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _offer_name(offer: dict[str, Any]) -> str:
    return str(offer.get("merchant_name") or offer.get("merchant_id") or "Продавец").strip()


def build_delivery_business_plan(
    *,
    own_price_kzt: Decimal | None,
    competitor_price_kzt: Decimal | None,
    market_offers: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    delivery_price_premium_kzt: Decimal | int | str | None,
    delivery_advantage_days: int | None,
    undercut_step_kzt: Decimal,
    page_visible_price_kzt: Decimal | None = None,
    top_visible_sellers: int = DEFAULT_VISIBLE_SELLERS,
) -> DeliveryBusinessPlan | None:
    """Turn ignored slow offers into a bounded commercial price opportunity.

    Slow nearby-priced sellers are still visible to the buyer, so they define
    how much premium a faster seller may charge. At the same time the target is
    capped strictly below the fifth external offer, which guarantees that our
    row stays inside the first five price-sorted sellers even when many sellers
    share the same price.
    """

    if own_price_kzt is None or not market_offers:
        return None
    if delivery_price_premium_kzt in (None, "") or delivery_advantage_days is None:
        return None

    premium = _optional_decimal(delivery_price_premium_kzt)
    if premium is None or premium < 0:
        return None
    advantage_days = max(1, int(delivery_advantage_days))
    visible_limit = max(1, int(top_visible_sellers))
    step = _money(undercut_step_kzt)
    page_floor = (
        None if page_visible_price_kzt is None else _money(page_visible_price_kzt)
    )

    ignored: list[tuple[Decimal, str, int]] = []
    external_prices: list[Decimal] = []
    for raw in market_offers:
        if not isinstance(raw, dict) or bool(raw.get("is_own")):
            continue
        price = _optional_money(raw.get("price_kzt"))
        if price is None:
            continue
        # Offers below the public headline were already marked by the scanner
        # as a different buyer/zone context. Do not let them distort TOP-5.
        if page_floor is not None and price < page_floor:
            continue
        external_prices.append(price)

        try:
            delivery_gap = int(raw.get("delivery_gap_days"))
        except (TypeError, ValueError, OverflowError):
            continue
        price_gap = _optional_decimal(raw.get("price_gap_kzt"))
        if price_gap is None:
            continue
        if delivery_gap >= advantage_days and abs(price_gap) <= premium:
            ignored.append((price, _offer_name(raw), delivery_gap))

    if not ignored:
        return None

    premium_ceiling = min(price + premium for price, _name, _gap in ignored)

    top5_ceiling: Decimal | None = None
    external_prices.sort()
    if len(external_prices) >= visible_limit:
        fifth_price = external_prices[visible_limit - 1]
        candidate = fifth_price - step
        if candidate > 0:
            top5_ceiling = candidate

    selected_ceiling = (
        None
        if competitor_price_kzt is None
        else _money(competitor_price_kzt) - step
    )
    ceilings = [premium_ceiling]
    if top5_ceiling is not None:
        ceilings.append(top5_ceiling)
    if selected_ceiling is not None and selected_ceiling > 0:
        ceilings.append(selected_ceiling)
    target_ceiling = min(ceilings)
    effective_competitor = target_ceiling + step

    # Keep the summary short enough for the CRM card while naming the shops the
    # user actually cares about. Prices are sorted, so the first names are the
    # strongest slow competitors.
    ignored.sort(key=lambda item: (item[0], item[1]))
    names: list[str] = []
    details: list[str] = []
    seen_names: set[str] = set()
    for price, name, gap in ignored:
        normalized = name.casefold()
        if normalized in seen_names:
            continue
        seen_names.add(normalized)
        names.append(name)
        if len(details) < 4:
            details.append(f"{name} ({format(price, 'f')} ₸, позже на {gap} дн.)")
    if len(names) > len(details):
        details.append(f"ещё {len(names) - len(details)}")

    parts = [
        "Исключены по доставке: " + ", ".join(details) + ".",
        (
            "Премия за быструю доставку допускает цену до "
            f"{format(premium_ceiling, 'f')} ₸."
        ),
    ]
    if top5_ceiling is not None:
        parts.append(
            f"Ограничение TOP-{visible_limit}: не выше {format(top5_ceiling, 'f')} ₸."
        )
    if selected_ceiling is not None and selected_ceiling == target_ceiling:
        parts.append(
            "Следующий допустимый конкурент задаёт более низкий ценовой ориентир."
        )
    parts.append(f"Коммерческий потолок: {format(target_ceiling, 'f')} ₸.")

    return DeliveryBusinessPlan(
        effective_competitor_price_kzt=effective_competitor,
        target_ceiling_kzt=target_ceiling,
        premium_ceiling_kzt=premium_ceiling,
        top5_ceiling_kzt=top5_ceiling,
        selected_competitor_ceiling_kzt=selected_ceiling,
        ignored_count=len(ignored),
        ignored_names=tuple(names),
        reason=" ".join(parts),
    )


def _decide_fast_price_core(
    *,
    own_price_kzt: Decimal | None,
    competitor_price_kzt: Decimal | None,
    safe_floor_kzt: Decimal,
    undercut_step_kzt: Decimal,
    allow_price_raise: bool,
    max_undercut_gap_percent: Decimal,
) -> FastPriceDecision:
    floor = _money(safe_floor_kzt)
    step = _money(undercut_step_kzt)
    max_gap = _percent(max_undercut_gap_percent)
    if floor <= 0:
        raise ValueError("safe_floor_kzt must be greater than zero")
    if step <= 0:
        raise ValueError("undercut_step_kzt must be greater than zero")
    if max_gap <= 0 or max_gap > 100:
        raise ValueError("max_undercut_gap_percent must be in (0, 100]")

    own = None if own_price_kzt is None else _money(own_price_kzt)
    competitor = (
        None if competitor_price_kzt is None else _money(competitor_price_kzt)
    )
    if competitor is None:
        return FastPriceDecision(
            safe_floor_kzt=floor,
            competitor_price_kzt=None,
            own_price_kzt=own,
            target_price_kzt=own,
            undercut_step_kzt=step,
            status="no_competitor",
            reason="Внешний конкурент не найден; цена не меняется.",
            write_allowed=False,
            max_undercut_gap_percent=max_gap,
        )

    gap_percent = None
    if own is not None and own > 0 and competitor < own:
        gap_percent = ((own - competitor) / own * Decimal("100")).quantize(
            PERCENT
        )
        if gap_percent > max_gap:
            return FastPriceDecision(
                safe_floor_kzt=floor,
                competitor_price_kzt=competitor,
                own_price_kzt=own,
                target_price_kzt=own,
                undercut_step_kzt=step,
                status="price_anomaly",
                reason=(
                    f"Конкурент ниже нашей цены на {gap_percent}%, что больше "
                    f"защитного порога {max_gap}%. Автозапись заблокирована."
                ),
                write_allowed=False,
                gap_percent=gap_percent,
                max_undercut_gap_percent=max_gap,
            )

    raw_target = competitor - step
    market_target = max(floor, raw_target)
    if own is not None and not allow_price_raise and market_target > own:
        target = own
        status = "hold_no_raise"
        reason = "Конкурент дороже, но автоматическое повышение отключено."
    else:
        target = market_target
        status = "floor_limited" if raw_target < floor else "ready"
        reason = (
            "Конкурент уже ниже безопасного порога; цена удерживается на floor."
            if status == "floor_limited"
            else "Цель равна цене лучшего подтверждённого конкурента минус шаг."
        )

    return FastPriceDecision(
        safe_floor_kzt=floor,
        competitor_price_kzt=competitor,
        own_price_kzt=own,
        target_price_kzt=_money(target),
        undercut_step_kzt=step,
        status=status,
        reason=reason,
        write_allowed=True,
        gap_percent=gap_percent,
        max_undercut_gap_percent=max_gap,
    )


def decide_fast_price(
    *,
    own_price_kzt: Decimal | None,
    competitor_price_kzt: Decimal | None,
    safe_floor_kzt: Decimal,
    undercut_step_kzt: Decimal,
    allow_price_raise: bool,
    max_undercut_gap_percent: Decimal,
    market_offers: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    delivery_price_premium_kzt: Decimal | int | str | None = None,
    delivery_advantage_days: int | None = None,
    page_visible_price_kzt: Decimal | None = None,
    top_visible_sellers: int = DEFAULT_VISIBLE_SELLERS,
) -> FastPriceDecision:
    """Use the proven lab rule plus delivery-premium and TOP-5 business guards."""

    step = _money(undercut_step_kzt)
    plan = build_delivery_business_plan(
        own_price_kzt=own_price_kzt,
        competitor_price_kzt=competitor_price_kzt,
        market_offers=market_offers,
        delivery_price_premium_kzt=delivery_price_premium_kzt,
        delivery_advantage_days=delivery_advantage_days,
        undercut_step_kzt=step,
        page_visible_price_kzt=page_visible_price_kzt,
        top_visible_sellers=top_visible_sellers,
    )
    effective_competitor = (
        competitor_price_kzt
        if plan is None
        else plan.effective_competitor_price_kzt
    )
    decision = _decide_fast_price_core(
        own_price_kzt=own_price_kzt,
        competitor_price_kzt=effective_competitor,
        safe_floor_kzt=safe_floor_kzt,
        undercut_step_kzt=step,
        allow_price_raise=allow_price_raise,
        max_undercut_gap_percent=max_undercut_gap_percent,
    )
    if plan is None:
        return decision

    status = decision.status
    if competitor_price_kzt is None and status == "ready":
        status = "delivery_advantage"
    return replace(
        decision,
        status=status,
        reason=f"{decision.reason} {plan.reason}",
    )
