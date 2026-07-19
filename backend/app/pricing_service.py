from __future__ import annotations

from decimal import Decimal, ROUND_CEILING

from sqlalchemy import select
from sqlalchemy.orm import Session

from .monitoring import SupplierOfferState
from .pricing_models import FxRateSnapshot, PriceCalculation, PriceCalculationStatus, PricingPolicy
from .suppliers import ProductBinding


HUNDRED = Decimal("100")
ONE = Decimal("1")


def _as_decimal(value: object | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _round_up(value: Decimal, step: int) -> Decimal:
    if step <= 0:
        raise ValueError("rounding_step_kzt must be positive")
    step_decimal = Decimal(step)
    return (value / step_decimal).to_integral_value(rounding=ROUND_CEILING) * step_decimal


def _latest_offer_for_product(session: Session, product_id: int) -> SupplierOfferState | None:
    return session.scalar(
        select(SupplierOfferState)
        .join(ProductBinding, ProductBinding.supplier_product_id == SupplierOfferState.supplier_product_id)
        .where(
            ProductBinding.product_id == product_id,
            ProductBinding.status.in_(["confirmed", "active", "degraded"]),
        )
        .order_by(ProductBinding.is_primary.desc(), ProductBinding.priority.asc(), SupplierOfferState.observed_at.desc())
        .limit(1)
    )


def _latest_fx(session: Session, base_currency: str) -> FxRateSnapshot | None:
    return session.scalar(
        select(FxRateSnapshot)
        .where(
            FxRateSnapshot.base_currency == base_currency,
            FxRateSnapshot.quote_currency == "KZT",
        )
        .order_by(FxRateSnapshot.observed_at.desc(), FxRateSnapshot.id.desc())
        .limit(1)
    )


def calculate_product_price(session: Session, *, product_id: int) -> PriceCalculation:
    policy = session.scalar(select(PricingPolicy).where(PricingPolicy.product_id == product_id))
    offer = _latest_offer_for_product(session, product_id)

    explanation: dict[str, object] = {
        "formula": "ceil_to_step((supplier_cost_kzt + delivery_cost_kzt + fixed_cost_kzt) / (1 - fees - margin))",
        "publication_mode": "recommendation_only",
    }

    if policy is None or not policy.enabled:
        calculation = PriceCalculation(
            product_id=product_id,
            pricing_policy_id=policy.id if policy else None,
            supplier_offer_state_id=offer.id if offer else None,
            status=PriceCalculationStatus.POLICY_DISABLED.value,
            explanation_json={**explanation, "reason": "pricing policy missing or disabled"},
        )
        session.add(calculation)
        session.flush()
        return calculation

    if offer is None or offer.price is None:
        calculation = PriceCalculation(
            product_id=product_id,
            pricing_policy_id=policy.id,
            status=PriceCalculationStatus.OFFER_MISSING.value,
            explanation_json={**explanation, "reason": "supplier offer state or price is missing"},
        )
        session.add(calculation)
        session.flush()
        return calculation

    if offer.available is False:
        calculation = PriceCalculation(
            product_id=product_id,
            pricing_policy_id=policy.id,
            supplier_offer_state_id=offer.id,
            status=PriceCalculationStatus.OFFER_UNAVAILABLE.value,
            supplier_price=offer.price,
            supplier_currency=offer.currency,
            explanation_json={**explanation, "reason": "supplier offer is unavailable"},
        )
        session.add(calculation)
        session.flush()
        return calculation

    currency = (offer.currency or "").strip().upper()
    if not currency:
        calculation = PriceCalculation(
            product_id=product_id,
            pricing_policy_id=policy.id,
            supplier_offer_state_id=offer.id,
            status=PriceCalculationStatus.CURRENCY_MISSING.value,
            supplier_price=offer.price,
            explanation_json={**explanation, "reason": "supplier currency is missing"},
        )
        session.add(calculation)
        session.flush()
        return calculation

    fx: FxRateSnapshot | None = None
    fx_rate = ONE
    if currency != "KZT":
        fx = _latest_fx(session, currency)
        if fx is None:
            calculation = PriceCalculation(
                product_id=product_id,
                pricing_policy_id=policy.id,
                supplier_offer_state_id=offer.id,
                status=PriceCalculationStatus.FX_MISSING.value,
                supplier_price=offer.price,
                supplier_currency=currency,
                explanation_json={**explanation, "reason": f"missing {currency}/KZT FX snapshot"},
            )
            session.add(calculation)
            session.flush()
            return calculation
        fx_rate = Decimal(str(fx.rate))

    margin_pct = Decimal(str(policy.target_margin_pct))
    total_fee_pct = Decimal(str(policy.marketplace_fee_pct)) + Decimal(str(policy.payment_fee_pct))
    denominator = ONE - ((margin_pct + total_fee_pct) / HUNDRED)
    if denominator <= 0:
        calculation = PriceCalculation(
            product_id=product_id,
            pricing_policy_id=policy.id,
            supplier_offer_state_id=offer.id,
            fx_rate_snapshot_id=fx.id if fx else None,
            status=PriceCalculationStatus.INVALID_POLICY.value,
            supplier_price=offer.price,
            supplier_currency=currency,
            fx_rate_to_kzt=fx_rate,
            total_fee_pct=total_fee_pct,
            target_margin_pct=margin_pct,
            explanation_json={**explanation, "reason": "fees plus margin must be below 100%"},
        )
        session.add(calculation)
        session.flush()
        return calculation

    supplier_cost_kzt = Decimal(str(offer.price)) * fx_rate
    delivery_cost = Decimal(str(policy.delivery_cost_kzt))
    fixed_cost = Decimal(str(policy.fixed_cost_kzt))
    raw_price = (supplier_cost_kzt + delivery_cost + fixed_cost) / denominator
    recommended = _round_up(raw_price, int(policy.rounding_step_kzt))
    minimum = _as_decimal(policy.minimum_price_kzt)
    if minimum is not None:
        recommended = max(recommended, minimum)
        recommended = _round_up(recommended, int(policy.rounding_step_kzt))

    calculation = PriceCalculation(
        product_id=product_id,
        pricing_policy_id=policy.id,
        supplier_offer_state_id=offer.id,
        fx_rate_snapshot_id=fx.id if fx else None,
        status=PriceCalculationStatus.READY.value,
        supplier_price=offer.price,
        supplier_currency=currency,
        fx_rate_to_kzt=fx_rate,
        supplier_cost_kzt=supplier_cost_kzt,
        delivery_cost_kzt=delivery_cost,
        fixed_cost_kzt=fixed_cost,
        total_fee_pct=total_fee_pct,
        target_margin_pct=margin_pct,
        recommended_price_kzt=recommended,
        explanation_json={
            **explanation,
            "supplier_price": str(offer.price),
            "supplier_currency": currency,
            "fx_rate_to_kzt": str(fx_rate),
            "supplier_cost_kzt": str(supplier_cost_kzt),
            "delivery_cost_kzt": str(delivery_cost),
            "fixed_cost_kzt": str(fixed_cost),
            "total_fee_pct": str(total_fee_pct),
            "target_margin_pct": str(margin_pct),
            "rounding_step_kzt": int(policy.rounding_step_kzt),
            "minimum_price_kzt": str(minimum) if minimum is not None else None,
            "recommended_price_kzt": str(recommended),
        },
    )
    session.add(calculation)
    session.flush()
    return calculation
