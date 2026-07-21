from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True, slots=True)
class SupplierCandidate:
    binding_id: int
    supplier_product_id: int
    supplier_code: str
    supplier_name: str
    price: Decimal | None
    currency: str | None
    available: bool | None
    delivery_days: int | None
    is_primary: bool
    priority: int
    last_checked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SupplierScore:
    binding_id: int
    supplier_product_id: int
    supplier_code: str
    supplier_name: str
    price_score: Decimal
    delivery_score: Decimal
    preference_score: Decimal
    freshness_score: Decimal
    total_score: Decimal
    eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BestOfferDecision:
    best: SupplierScore | None
    ranked: tuple[SupplierScore, ...]


class BestOfferEngine:
    """Rank current supplier states without owning monitoring or pricing policy.

    Availability and a positive price are hard eligibility constraints. Eligible
    offers are scored relative to the other current offers for the same product.
    The engine is deterministic, side-effect free and contains no database, HTTP,
    browser, XML or marketplace dependencies.
    """

    PRICE_WEIGHT = Decimal("55")
    DELIVERY_WEIGHT = Decimal("25")
    PREFERENCE_WEIGHT = Decimal("15")
    FRESHNESS_WEIGHT = Decimal("5")
    MAX_DELIVERY_DAYS = 10
    FRESH_HOURS = 24

    @classmethod
    def decide(
        cls,
        candidates: Iterable[SupplierCandidate],
        *,
        now: datetime | None = None,
    ) -> BestOfferDecision:
        items = tuple(candidates)
        eligible = tuple(
            item
            for item in items
            if item.available is True and item.price is not None and item.price > 0
        )
        min_price = min((item.price for item in eligible if item.price is not None), default=None)
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)

        scores = tuple(cls._score(item, min_price=min_price, now=current) for item in items)
        ranked = tuple(
            sorted(
                scores,
                key=lambda score: (
                    not score.eligible,
                    -score.total_score,
                    score.binding_id,
                ),
            )
        )
        best = next((score for score in ranked if score.eligible), None)
        return BestOfferDecision(best=best, ranked=ranked)

    @classmethod
    def _score(
        cls,
        candidate: SupplierCandidate,
        *,
        min_price: Decimal | None,
        now: datetime,
    ) -> SupplierScore:
        reasons: list[str] = []
        eligible = (
            candidate.available is True
            and candidate.price is not None
            and candidate.price > 0
        )
        if not eligible:
            if candidate.available is False:
                reasons.append("Товар отсутствует у поставщика")
            elif candidate.available is not True:
                reasons.append("Наличие не подтверждено")
            if candidate.price is None or candidate.price <= 0:
                reasons.append("Нет подтверждённой цены")
            return SupplierScore(
                binding_id=candidate.binding_id,
                supplier_product_id=candidate.supplier_product_id,
                supplier_code=candidate.supplier_code,
                supplier_name=candidate.supplier_name,
                price_score=Decimal("0"),
                delivery_score=Decimal("0"),
                preference_score=Decimal("0"),
                freshness_score=Decimal("0"),
                total_score=Decimal("0"),
                eligible=False,
                reasons=tuple(reasons),
            )

        price = candidate.price or Decimal("0")
        price_score = (
            cls.PRICE_WEIGHT
            if min_price is not None and price == min_price
            else cls._round(cls.PRICE_WEIGHT * (min_price or price) / price)
        )
        if min_price is not None and price == min_price:
            reasons.append("Самая низкая подтверждённая цена")
        else:
            reasons.append("Цена учтена относительно лучшего предложения")

        if candidate.delivery_days is None:
            delivery_score = Decimal("0")
            reasons.append("Срок доставки не подтверждён")
        else:
            days = max(0, min(int(candidate.delivery_days), cls.MAX_DELIVERY_DAYS))
            delivery_score = cls._round(
                cls.DELIVERY_WEIGHT
                * Decimal(cls.MAX_DELIVERY_DAYS - days)
                / Decimal(cls.MAX_DELIVERY_DAYS)
            )
            reasons.append(
                "Доставка сегодня"
                if days == 0
                else f"Доставка за {candidate.delivery_days} дн."
            )

        primary_score = Decimal("10") if candidate.is_primary else Decimal("0")
        priority_value = max(0, min(int(candidate.priority), 100))
        priority_score = cls._round(Decimal("5") * Decimal(100 - priority_value) / Decimal(100))
        preference_score = min(cls.PREFERENCE_WEIGHT, primary_score + priority_score)
        if candidate.is_primary:
            reasons.append("Основная привязка")
        elif priority_score > 0:
            reasons.append("Учитывается ручной приоритет")

        freshness_score = Decimal("0")
        if candidate.last_checked_at is not None:
            checked = candidate.last_checked_at
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=UTC)
            age_hours = max(Decimal("0"), Decimal(str((now - checked).total_seconds())) / Decimal("3600"))
            if age_hours <= cls.FRESH_HOURS:
                freshness_score = cls.FRESHNESS_WEIGHT
                reasons.append("Проверено за последние 24 часа")

        total = cls._round(price_score + delivery_score + preference_score + freshness_score)
        return SupplierScore(
            binding_id=candidate.binding_id,
            supplier_product_id=candidate.supplier_product_id,
            supplier_code=candidate.supplier_code,
            supplier_name=candidate.supplier_name,
            price_score=price_score,
            delivery_score=delivery_score,
            preference_score=preference_score,
            freshness_score=freshness_score,
            total_score=total,
            eligible=True,
            reasons=tuple(reasons),
        )

    @staticmethod
    def _round(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"))
