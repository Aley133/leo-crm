from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from math import ceil
from typing import Iterable


_DELIVERED = {"delivered"}
_ACTIVE = {"new", "accepted", "assembly", "shipping"}
_CANCELLED = {"cancelled", "returned"}


@dataclass(frozen=True, slots=True)
class ProductOrderLineFact:
    order_id: int
    status: str
    quantity: int
    line_total: Decimal
    ordered_at: datetime | None
    delivered_at: datetime | None


@dataclass(frozen=True, slots=True)
class CommerceWindow:
    days: int
    orders_count: int
    units_ordered: int
    units_delivered: int
    active_units: int
    cancelled_units: int
    delivered_revenue: Decimal
    average_sale_price: Decimal | None
    estimated_procurement_cost: Decimal | None
    estimated_gross_profit_before_fees: Decimal | None
    estimated_gross_margin_pct_before_fees: Decimal | None


@dataclass(frozen=True, slots=True)
class PurchaseRecommendation:
    mode: str
    title: str
    reason: str
    target_stock_units: int
    daily_velocity_30d: Decimal
    coverage_days: int
    confidence: str


@dataclass(frozen=True, slots=True)
class ProductCommerceAnalysis:
    windows: tuple[CommerceWindow, ...]
    current_unit_cost: Decimal | None
    cost_source: str | None
    recommendation: PurchaseRecommendation
    profit_is_estimated: bool = True


class ProductCommerceAnalyzer:
    """Pure read-model service for product demand and provisional economics.

    Revenue is realised only for delivered orders. Until Warehouse/FIFO exists,
    cost is deliberately estimated from the current best supplier offer and the
    result is named `before_fees` to avoid presenting it as accounting profit.
    """

    @classmethod
    def analyze(
        cls,
        facts: Iterable[ProductOrderLineFact],
        *,
        current_unit_cost: Decimal | None,
        cost_source: str | None,
        now: datetime | None = None,
        windows: tuple[int, ...] = (7, 30, 90),
        coverage_days: int = 14,
    ) -> ProductCommerceAnalysis:
        observed_now = now or datetime.now(UTC)
        rows = tuple(facts)
        snapshots = tuple(
            cls._window(
                rows,
                days=days,
                now=observed_now,
                current_unit_cost=current_unit_cost,
            )
            for days in windows
        )
        by_days = {window.days: window for window in snapshots}
        recommendation = cls._recommend(
            week=by_days.get(7),
            month=by_days.get(30),
            coverage_days=coverage_days,
        )
        return ProductCommerceAnalysis(
            windows=snapshots,
            current_unit_cost=current_unit_cost,
            cost_source=cost_source,
            recommendation=recommendation,
        )

    @classmethod
    def _window(
        cls,
        facts: tuple[ProductOrderLineFact, ...],
        *,
        days: int,
        now: datetime,
        current_unit_cost: Decimal | None,
    ) -> CommerceWindow:
        cutoff = now - timedelta(days=days)
        included = tuple(
            fact
            for fact in facts
            if fact.ordered_at is not None and cls._aware(fact.ordered_at) >= cutoff
        )
        orders_count = len({fact.order_id for fact in included})
        units_ordered = sum(
            fact.quantity for fact in included if fact.status not in _CANCELLED
        )
        units_delivered = sum(
            fact.quantity for fact in included if fact.status in _DELIVERED
        )
        active_units = sum(fact.quantity for fact in included if fact.status in _ACTIVE)
        cancelled_units = sum(
            fact.quantity for fact in included if fact.status in _CANCELLED
        )
        revenue = sum(
            (fact.line_total for fact in included if fact.status in _DELIVERED),
            start=Decimal("0"),
        )
        average_sale_price = (
            cls._money(revenue / units_delivered) if units_delivered else None
        )
        procurement_cost = (
            cls._money(current_unit_cost * units_delivered)
            if current_unit_cost is not None and units_delivered
            else None
        )
        gross_profit = (
            cls._money(revenue - procurement_cost)
            if procurement_cost is not None
            else None
        )
        margin_pct = (
            cls._percent(gross_profit / revenue * Decimal("100"))
            if gross_profit is not None and revenue > 0
            else None
        )
        return CommerceWindow(
            days=days,
            orders_count=orders_count,
            units_ordered=units_ordered,
            units_delivered=units_delivered,
            active_units=active_units,
            cancelled_units=cancelled_units,
            delivered_revenue=cls._money(revenue),
            average_sale_price=average_sale_price,
            estimated_procurement_cost=procurement_cost,
            estimated_gross_profit_before_fees=gross_profit,
            estimated_gross_margin_pct_before_fees=margin_pct,
        )

    @classmethod
    def _recommend(
        cls,
        *,
        week: CommerceWindow | None,
        month: CommerceWindow | None,
        coverage_days: int,
    ) -> PurchaseRecommendation:
        units_7 = 0 if week is None else week.units_ordered
        units_30 = 0 if month is None else month.units_ordered
        velocity = Decimal(units_30) / Decimal("30")
        target = ceil(float(velocity * coverage_days)) if units_30 else 0

        if units_30 >= 10 or units_7 >= 4:
            return PurchaseRecommendation(
                mode="stock",
                title="Рекомендуется держать товар на складе",
                reason="Спрос устойчивый: карточка продаётся достаточно часто для складского запаса.",
                target_stock_units=max(target, 3),
                daily_velocity_30d=cls._velocity(velocity),
                coverage_days=coverage_days,
                confidence="high" if units_30 >= 10 else "medium",
            )
        if units_30 >= 3 or units_7 >= 2:
            return PurchaseRecommendation(
                mode="trial_batch",
                title="Рекомендуется пробная партия",
                reason="Продажи уже повторяются, но данных пока недостаточно для большого запаса.",
                target_stock_units=max(target, 2),
                daily_velocity_30d=cls._velocity(velocity),
                coverage_days=coverage_days,
                confidence="medium",
            )
        return PurchaseRecommendation(
            mode="preorder",
            title="Оставить товар на предзаказе",
            reason="Продажи редкие или данных ещё мало; закупка партии пока заморозит деньги.",
            target_stock_units=0,
            daily_velocity_30d=cls._velocity(velocity),
            coverage_days=coverage_days,
            confidence="low" if units_30 else "none",
        )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @staticmethod
    def _money(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @staticmethod
    def _percent(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)

    @staticmethod
    def _velocity(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
