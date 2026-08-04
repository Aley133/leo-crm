from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


MONEY = Decimal("0.01")
PERCENT = Decimal("0.01")
KASPI_COMMISSION_RATE = Decimal("0.12")
TAX_RATE = Decimal("0.03")


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def kaspi_logistics_per_unit(unit_sale_price: Decimal) -> Decimal:
    """Return the fixed Kaspi logistics tariff for one sold unit.

    Business tariff supplied by the product owner. Boundary values are inclusive
    at the lower edge of every subsequent band.
    """

    price = Decimal(unit_sale_price)
    if price < Decimal("1000"):
        return Decimal("57")
    if price < Decimal("3000"):
        return Decimal("173")
    if price < Decimal("5000"):
        return Decimal("231")
    if price < Decimal("10000"):
        return Decimal("927")
    return Decimal("1507")


def allocate_order_logistics(
    *,
    order_total: Decimal,
    line_totals: tuple[Decimal, ...],
) -> tuple[Decimal, ...]:
    """Allocate one Kaspi logistics tariff across all lines in an order.

    Kaspi charges the tariff once from the combined order total. Commerce still
    exposes per-line profit, so the single tariff is distributed proportionally
    and the final positive line receives the rounding residue.
    """

    if not line_totals:
        return ()

    logistics_total = _money(kaspi_logistics_per_unit(Decimal(order_total)))
    positive_totals = tuple(
        max(Decimal(value), Decimal("0")) for value in line_totals
    )
    allocation_base = sum(positive_totals, Decimal("0"))
    if allocation_base <= 0:
        return (
            logistics_total,
            *(Decimal("0.00") for _ in line_totals[1:]),
        )

    final_positive_index = max(
        index for index, value in enumerate(positive_totals) if value > 0
    )
    remaining = logistics_total
    allocated: list[Decimal] = []
    for index, line_total in enumerate(positive_totals):
        if line_total <= 0:
            share = Decimal("0.00")
        elif index == final_positive_index:
            share = remaining
        else:
            share = _money(logistics_total * line_total / allocation_base)
        allocated.append(share)
        remaining -= share
    return tuple(allocated)


@dataclass(frozen=True, slots=True)
class LineEconomics:
    revenue: Decimal
    procurement_cost: Decimal
    kaspi_commission: Decimal
    tax: Decimal
    logistics: Decimal
    net_profit: Decimal
    net_margin_pct: Decimal


def calculate_line_economics(
    *,
    unit_sale_price: Decimal,
    quantity: int,
    procurement_unit_cost: Decimal,
    logistics_cost: Decimal | None = None,
) -> LineEconomics:
    if quantity < 0:
        raise ValueError("quantity must not be negative")

    unit_price = Decimal(unit_sale_price)
    unit_cost = Decimal(procurement_unit_cost)
    revenue = _money(unit_price * quantity)
    procurement_cost = _money(unit_cost * quantity)
    commission = _money(revenue * KASPI_COMMISSION_RATE)
    tax = _money(revenue * TAX_RATE)
    logistics = _money(
        kaspi_logistics_per_unit(unit_price) * quantity
        if logistics_cost is None
        else Decimal(logistics_cost)
    )
    net_profit = _money(revenue - procurement_cost - commission - tax - logistics)
    margin = (
        Decimal("0")
        if revenue <= 0
        else (net_profit / revenue * Decimal("100")).quantize(PERCENT, rounding=ROUND_HALF_UP)
    )
    return LineEconomics(
        revenue=revenue,
        procurement_cost=procurement_cost,
        kaspi_commission=commission,
        tax=tax,
        logistics=logistics,
        net_profit=net_profit,
        net_margin_pct=margin,
    )
