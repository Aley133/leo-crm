from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


MONEY = Decimal("0.01")
PERCENT = Decimal("0.01")


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


def _money(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(MONEY)


def _percent(value: Decimal | int | str) -> Decimal:
    return Decimal(str(value)).quantize(PERCENT)


def decide_fast_price(
    *,
    own_price_kzt: Decimal | None,
    competitor_price_kzt: Decimal | None,
    safe_floor_kzt: Decimal,
    undercut_step_kzt: Decimal,
    allow_price_raise: bool,
    max_undercut_gap_percent: Decimal,
) -> FastPriceDecision:
    """Use the proven lab rule while keeping CRM's calculated floor authoritative."""

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
