from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


MONEY = Decimal("0.01")
PERCENT = Decimal("0.01")
KASPI_COMMISSION_RATE = Decimal("0.125")
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
) -> LineEconomics:
    if quantity < 0:
        raise ValueError("quantity must not be negative")

    unit_price = Decimal(unit_sale_price)
    unit_cost = Decimal(procurement_unit_cost)
    revenue = _money(unit_price * quantity)
    procurement_cost = _money(unit_cost * quantity)
    commission = _money(revenue * KASPI_COMMISSION_RATE)
    tax = _money(revenue * TAX_RATE)
    logistics = _money(kaspi_logistics_per_unit(unit_price) * quantity)
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
