from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from .commerce.profit_calculator import kaspi_logistics_per_unit


MONEY = Decimal("0.01")
PERCENT = Decimal("0.01")
DEFAULT_VISIBLE_SELLERS = 5
LOGISTICS_PRICE_BREAKS = (
    Decimal("1000"),
    Decimal("3000"),
    Decimal("5000"),
    Decimal("10000"),
)


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
    """Commercial ceiling created by the faster-delivery advantage."""

    effective_competitor_price_kzt: Decimal
    target_ceiling_kzt: Decimal
    premium_ceiling_kzt: Decimal
    top5_ceiling_kzt: Decimal | None
    selected_competitor_ceiling_kzt: Decimal | None
    ignored_count: int
    ignored_names: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class LogisticsJump:
    original_target_kzt: Decimal
    target_price_kzt: Decimal
    threshold_kzt: Decimal
    upper_bound_kzt: Decimal
    logistics_kzt: Decimal


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


def optimize_target_for_logistics_jump(
    *,
    target_price_kzt: Decimal,
    safe_floor_kzt: Decimal,
) -> LogisticsJump | None:
    """Skip a price window made unattractive by a fixed Kaspi logistics jump.

    The owner treats the new fixed logistics tariff as an amount that must first
    be earned back after crossing a tariff boundary. Example: at 5,000 KZT the
    tariff becomes 927 KZT, so a market target inside 5,000..5,927 is replaced
    by 4,999 when that price still satisfies the configured minimum-profit
    floor. The same rule is derived from the authoritative tariff table for all
    boundaries instead of hard-coding one product-specific threshold.
    """

    target = _money(target_price_kzt)
    floor = _money(safe_floor_kzt)
    for threshold in reversed(LOGISTICS_PRICE_BREAKS):
        landing = _money(threshold - Decimal("1"))
        logistics = _money(kaspi_logistics_per_unit(threshold))
        upper_bound = _money(threshold + logistics)
        if threshold <= target <= upper_bound and floor <= landing:
            return LogisticsJump(
                original_target_kzt=target,
                target_price_kzt=landing,
                threshold_kzt=_money(threshold),
                upper_bound_kzt=upper_bound,
                logistics_kzt=logistics,
            )
    return None


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
    """Turn ignored slow offers into a bounded commercial price opportunity."""

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
    page_floor = None if page_visible_price_kzt is None else _money(page_visible_price_kzt)

    ignored: list[tuple[Decimal, str, int]] = []
    external_prices: list[Decimal] = []
    for raw in market_offers:
        if (
            not isinstance(raw, dict)
            or bool(raw.get("is_own"))
            or bool(raw.get("is_owned_group"))
        ):
            continue
        price = _optional_money(raw.get("price_kzt"))
        if price is None:
            continue
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

    selected_ceiling = None if competitor_price_kzt is None else _money(competitor_price_kzt) - step
    ceilings = [premium_ceiling]
    if top5_ceiling is not None:
        ceilings.append(top5_ceiling)
    if selected_ceiling is not None and selected_ceiling > 0:
        ceilings.append(selected_ceiling)
    target_ceiling = min(ceilings)
    effective_competitor = target_ceiling + step

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
        f"Премия за быструю доставку допускает цену до {format(premium_ceiling, 'f')} ₸.",
    ]
    if top5_ceiling is not None:
        parts.append(f"Ограничение TOP-{visible_limit}: не выше {format(top5_ceiling, 'f')} ₸.")
    if selected_ceiling is not None and selected_ceiling == target_ceiling:
        parts.append("Следующий допустимый конкурент задаёт более низкий ценовой ориентир.")
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


def _owned_peer_prices(
    market_offers: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
) -> list[tuple[Decimal, str]]:
    peers: list[tuple[Decimal, str]] = []
    for raw in market_offers or ():
        if not isinstance(raw, dict) or bool(raw.get("is_own")):
            continue
        if not (bool(raw.get("is_owned_peer")) or bool(raw.get("is_owned_group"))):
            continue
        price = _optional_money(raw.get("price_kzt"))
        if price is not None:
            peers.append((price, _offer_name(raw)))
    peers.sort(key=lambda item: (item[0], item[1].casefold()))
    return peers


def external_anchor_price(
    market_offers: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    *,
    page_visible_price_kzt: Decimal | None,
) -> Decimal | None:
    page_floor = (
        None if page_visible_price_kzt is None else _money(page_visible_price_kzt)
    )
    prices: list[Decimal] = []
    for raw in market_offers or ():
        if not isinstance(raw, dict) or bool(raw.get("is_owned_group")):
            continue
        if bool(raw.get("is_own")) or bool(raw.get("is_owned_peer")):
            continue
        price = _optional_money(raw.get("price_kzt"))
        if price is None or (page_floor is not None and price < page_floor):
            continue
        prices.append(price)
    return min(prices) if prices else None


def _decide_owned_group_price(
    *,
    own_price_kzt: Decimal | None,
    external_anchor_price_kzt: Decimal | None,
    effective_competitor_price_kzt: Decimal | None,
    safe_floor_kzt: Decimal,
    undercut_step_kzt: Decimal,
    max_undercut_gap_percent: Decimal,
    market_offers: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    owned_price_band_kzt: Decimal | int | str | None,
    owned_cycle_anchor_price_kzt: Decimal | int | str | None,
) -> FastPriceDecision | None:
    """Coordinate owned shops around an external or durable internal anchor.

    Inside the configured band the shops may alternate the ordinary one-step
    undercut. Once either owned row moves below that band, both agents receive
    the same reset target. If all external sellers disappear, the last anchor
    is preserved so the owned shops continue the cycle instead of stopping.
    """

    peers = _owned_peer_prices(market_offers)
    band = _optional_decimal(owned_price_band_kzt)
    if not peers or band is None or band <= 0:
        return None

    peer_price, peer_name = peers[0]
    own = None if own_price_kzt is None else _money(own_price_kzt)
    floor = _money(safe_floor_kzt)
    band_money = _money(band)
    group_prices = [peer_price]
    if own is not None:
        group_prices.append(own)
    external = (
        None
        if external_anchor_price_kzt is None
        else _money(external_anchor_price_kzt)
    )
    saved_anchor = _optional_money(owned_cycle_anchor_price_kzt)
    # Without an external seller both stores must still complete the same
    # bounded cycle. The durable anchor comes from FastDumpingState; on the
    # first scan the highest current owned price becomes the cycle start.
    anchor = external or max(saved_anchor or Decimal("0"), max(group_prices))
    group_min = min(group_prices)
    reset_boundary = _money(anchor - band_money)

    # The safe floor always wins. A market already below floor is handled by
    # the ordinary floor-limited decision and cannot trigger an upward cycle.
    if anchor > floor and group_min < reset_boundary:
        anchor_kind = (
            "внешнему ориентиру"
            if external is not None
            else "сохранённому старту цикла"
        )
        return FastPriceDecision(
            safe_floor_kzt=floor,
            competitor_price_kzt=anchor,
            own_price_kzt=own,
            target_price_kzt=anchor,
            undercut_step_kzt=_money(undercut_step_kzt),
            status="owned_group_reset",
            reason=(
                f"LeoXpress/BARWORK прошли кооперативный коридор "
                f"{format(band_money, 'f')} ₸ ниже цены "
                f"{format(anchor, 'f')} ₸. Цена возвращается к {anchor_kind}; "
                "после синхронизации цикл начнётся заново."
            ),
            write_allowed=True,
            max_undercut_gap_percent=_percent(max_undercut_gap_percent),
        )

    active_competitor = peer_price
    if external is not None and effective_competitor_price_kzt is not None:
        active_competitor = min(
            _money(effective_competitor_price_kzt),
            peer_price,
        )
    decision = _decide_fast_price_core(
        own_price_kzt=own_price_kzt,
        competitor_price_kzt=active_competitor,
        safe_floor_kzt=safe_floor_kzt,
        undercut_step_kzt=undercut_step_kzt,
        # A controlled return within the owned-shop cycle is not an ordinary
        # market price raise, so it must remain possible even when following
        # an external market increase is disabled.
        allow_price_raise=True,
        max_undercut_gap_percent=max_undercut_gap_percent,
    )
    if external is None or peer_price < anchor:
        # Keep Kaspi's logistics-price shortcut inside the cooperative cycle.
        # At the lower edge we intentionally allow the final ordinary step
        # (for example 3200 -> 3199) so that the next scan deterministically
        # triggers the reset, but never a tariff jump such as 3200 -> 2999.
        last_cycle_step = max(
            floor,
            _money(reset_boundary - _money(undercut_step_kzt)),
        )
        if (
            decision.write_allowed
            and decision.target_price_kzt is not None
            and decision.target_price_kzt < last_cycle_step
        ):
            bounded_gap = decision.gap_percent
            if own is not None and own > 0 and last_cycle_step < own:
                bounded_gap = ((own - last_cycle_step) / own * Decimal("100")).quantize(
                    PERCENT
                )
            decision = replace(
                decision,
                target_price_kzt=last_cycle_step,
                status="owned_group_band",
                reason=(
                    "Логистический скачок ограничен последним шагом "
                    f"кооперативного коридора: {format(last_cycle_step, 'f')} ₸."
                ),
                gap_percent=bounded_gap,
            )
        return replace(
            decision,
            status=(
                "owned_group_band" if decision.status == "ready" else decision.status
            ),
            reason=(
                f"Свой магазин {peer_name} временно задаёт шаг внутри коридора "
                f"{format(band_money, 'f')} ₸. "
                f"{'Внешний ориентир' if external is not None else 'Старт цикла'}: "
                f"{format(anchor, 'f')} ₸; граница возврата: "
                f"{format(reset_boundary, 'f')} ₸. {decision.reason}"
            ),
        )
    return replace(
        decision,
        reason=(
            f"Внешний продавец остаётся ближайшим ценовым ориентиром; "
            f"свой магазин {peer_name} не заставляет цену снижаться. "
            f"{decision.reason}"
        ),
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
    competitor = None if competitor_price_kzt is None else _money(competitor_price_kzt)
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

    competitor_gap = None
    if own is not None and own > 0 and competitor < own:
        competitor_gap = ((own - competitor) / own * Decimal("100")).quantize(PERCENT)
        if competitor_gap > max_gap:
            return FastPriceDecision(
                safe_floor_kzt=floor,
                competitor_price_kzt=competitor,
                own_price_kzt=own,
                target_price_kzt=own,
                undercut_step_kzt=step,
                status="price_anomaly",
                reason=(
                    f"Конкурент ниже нашей цены на {competitor_gap}%, что больше "
                    f"защитного порога {max_gap}%. Автозапись заблокирована."
                ),
                write_allowed=False,
                gap_percent=competitor_gap,
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

        # The tariff jump is a shortcut only while moving the price downward.
        # When the market moves up, Fast must follow the ordinary competitor
        # target instead of falling back below the previous tariff boundary.
        logistics_jump = (
            optimize_target_for_logistics_jump(
                target_price_kzt=target,
                safe_floor_kzt=floor,
            )
            if own is not None and target < own
            else None
        )
        if logistics_jump is not None:
            target = logistics_jump.target_price_kzt
            status = "logistics_jump"
            reason = (
                f"Логистический скачок Kaspi: расчётная цель "
                f"{format(logistics_jump.original_target_kzt, 'f')} ₸ попала в "
                f"невыгодный диапазон {format(logistics_jump.threshold_kzt, 'f')}–"
                f"{format(logistics_jump.upper_bound_kzt, 'f')} ₸ с логистикой "
                f"{format(logistics_jump.logistics_kzt, 'f')} ₸. Цель перенесена "
                f"на {format(logistics_jump.target_price_kzt, 'f')} ₸; "
                "минимальная прибыль сохранена."
            )

    target = _money(target)
    target_gap = competitor_gap
    if own is not None and own > 0 and target < own:
        target_gap = ((own - target) / own * Decimal("100")).quantize(PERCENT)
        if target_gap > max_gap:
            return FastPriceDecision(
                safe_floor_kzt=floor,
                competitor_price_kzt=competitor,
                own_price_kzt=own,
                target_price_kzt=own,
                undercut_step_kzt=step,
                status="price_anomaly",
                reason=(
                    f"Итоговая цель потребовала бы снизить цену на {target_gap}%, "
                    f"что больше защитного порога {max_gap}%. Автозапись заблокирована."
                ),
                write_allowed=False,
                gap_percent=target_gap,
                max_undercut_gap_percent=max_gap,
            )

    return FastPriceDecision(
        safe_floor_kzt=floor,
        competitor_price_kzt=competitor,
        own_price_kzt=own,
        target_price_kzt=target,
        undercut_step_kzt=step,
        status=status,
        reason=reason,
        write_allowed=True,
        gap_percent=target_gap,
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
    owned_price_band_kzt: Decimal | int | str | None = None,
    owned_cycle_anchor_price_kzt: Decimal | int | str | None = None,
) -> FastPriceDecision:
    """Use the proven lab rule plus delivery-premium, TOP-5 and tariff guards."""

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
    effective_competitor = competitor_price_kzt if plan is None else plan.effective_competitor_price_kzt
    decision = _decide_owned_group_price(
        own_price_kzt=own_price_kzt,
        external_anchor_price_kzt=external_anchor_price(
            market_offers,
            page_visible_price_kzt=page_visible_price_kzt,
        ),
        effective_competitor_price_kzt=effective_competitor,
        safe_floor_kzt=safe_floor_kzt,
        undercut_step_kzt=step,
        max_undercut_gap_percent=max_undercut_gap_percent,
        market_offers=market_offers,
        owned_price_band_kzt=owned_price_band_kzt,
        owned_cycle_anchor_price_kzt=owned_cycle_anchor_price_kzt,
    ) or _decide_fast_price_core(
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
