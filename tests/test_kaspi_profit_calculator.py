from decimal import Decimal

import pytest

from backend.app.commerce.profit_calculator import (
    calculate_line_economics,
    kaspi_logistics_per_unit,
)


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        ("0", "57"),
        ("999.99", "57"),
        ("1000", "173"),
        ("2999.99", "173"),
        ("3000", "231"),
        ("4999.99", "231"),
        ("5000", "927"),
        ("9999.99", "927"),
        ("10000", "1507"),
        ("50000", "1507"),
    ],
)
def test_kaspi_logistics_tariff_boundaries(price: str, expected: str) -> None:
    assert kaspi_logistics_per_unit(Decimal(price)) == Decimal(expected)


def test_line_economics_uses_12_5_percent_commission_and_3_percent_tax() -> None:
    result = calculate_line_economics(
        unit_sale_price=Decimal("1499"),
        quantity=1,
        procurement_unit_cost=Decimal("700"),
    )

    assert result.revenue == Decimal("1499.00")
    assert result.procurement_cost == Decimal("700.00")
    assert result.kaspi_commission == Decimal("187.38")
    assert result.tax == Decimal("44.97")
    assert result.logistics == Decimal("173.00")
    assert result.net_profit == Decimal("393.65")
    assert result.net_margin_pct == Decimal("26.26")


def test_logistics_is_charged_per_unit() -> None:
    result = calculate_line_economics(
        unit_sale_price=Decimal("3600"),
        quantity=2,
        procurement_unit_cost=Decimal("1000"),
    )
    assert result.logistics == Decimal("462.00")
