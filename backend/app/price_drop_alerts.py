from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from statistics import median

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .models import OutboxEvent, Product
from .monitoring import (
    MonitorAttempt,
    MonitorTarget,
    SupplierOfferObservation,
)
from .suppliers import ProductBinding, Supplier, SupplierProduct


PRICE_DROP_EVENT_TYPE = "supplier.price_drop_detected"
PRICE_DROP_THRESHOLD = Decimal("0.50")
PRICE_BASELINE_WINDOW = 6


@dataclass(frozen=True, slots=True)
class PriceDropAlert:
    event_id: object
    supplier_product_id: int
    baseline_price: Decimal
    current_price: Decimal
    drop_percent: Decimal
    sample_size: int


def _historical_prices(
    session: Session,
    *,
    observation: SupplierOfferObservation,
) -> list[Decimal]:
    currency_filter = (
        SupplierOfferObservation.currency.is_(None)
        if observation.currency is None
        else or_(
            SupplierOfferObservation.currency == observation.currency,
            SupplierOfferObservation.currency.is_(None),
        )
    )
    rows = session.scalars(
        select(SupplierOfferObservation.price)
        .where(
            SupplierOfferObservation.supplier_product_id == observation.supplier_product_id,
            SupplierOfferObservation.id != observation.id,
            SupplierOfferObservation.price.is_not(None),
            SupplierOfferObservation.price > 0,
            or_(
                SupplierOfferObservation.available.is_(True),
                SupplierOfferObservation.available.is_(None),
            ),
            currency_filter,
        )
        .order_by(
            SupplierOfferObservation.observed_at.desc(),
            SupplierOfferObservation.id.desc(),
        )
        .limit(PRICE_BASELINE_WINDOW)
    ).all()
    return [Decimal(str(price)) for price in rows if price is not None]


def _alert_context(
    session: Session,
    observation: SupplierOfferObservation,
) -> dict[str, object] | None:
    row = session.execute(
        select(
            SupplierProduct,
            Supplier,
            ProductBinding,
            Product,
        )
        .select_from(MonitorAttempt)
        .join(MonitorTarget, MonitorTarget.id == MonitorAttempt.monitor_target_id)
        .join(
            ProductBinding,
            ProductBinding.id == MonitorTarget.product_binding_id,
        )
        .join(Product, Product.id == ProductBinding.product_id)
        .join(
            SupplierProduct,
            SupplierProduct.id == ProductBinding.supplier_product_id,
        )
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .where(
            MonitorAttempt.id == observation.monitor_attempt_id,
            SupplierProduct.id == observation.supplier_product_id,
        )
    ).one_or_none()
    if row is None:
        return None
    supplier_product, supplier, binding, product = row
    return {
        "price_drop_alert_enabled": product.sudden_price_alert_enabled,
        "product_id": product.id,
        "product_name": product.name,
        "merchant_sku": product.merchant_sku,
        "kaspi_product_id": product.kaspi_product_id,
        "supplier_code": supplier.code,
        "supplier_name": supplier.name,
        "supplier_product_title": supplier_product.title,
        "supplier_product_url": supplier_product.url,
        "binding_id": binding.id,
    }


def enqueue_price_drop_alert(
    session: Session,
    *,
    observation: SupplierOfferObservation,
) -> PriceDropAlert | None:
    """Create a transactional alert event for a new, available price cliff.

    The median of the six latest valid prices is used as the normal-price
    baseline. A low-price state emits only once: other observation changes at
    the same already-low price do not create repeated Telegram notifications.
    """
    if (
        observation.price is None
        or observation.price <= 0
        or observation.available is False
        or observation.stock == 0
    ):
        return None

    context = _alert_context(session, observation)
    if context is None or not context.pop("price_drop_alert_enabled"):
        return None

    prices = _historical_prices(session, observation=observation)
    if not prices:
        return None

    current_price = Decimal(str(observation.price))
    baseline_price = Decimal(str(median(prices)))
    alert_price_ceiling = baseline_price * (Decimal("1") - PRICE_DROP_THRESHOLD)
    if current_price > alert_price_ceiling:
        return None

    # The latest valid observation is the state transition guard. Once a price
    # is already in the alert zone, stock/delivery/seller changes must not spam
    # another notification. A recovery above the threshold arms the next drop.
    latest_price = prices[0]
    if latest_price <= alert_price_ceiling:
        return None

    drop_percent = (
        (baseline_price - current_price)
        / baseline_price
        * Decimal("100")
    ).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    event = OutboxEvent(
        aggregate_type="supplier_product",
        aggregate_id=str(observation.supplier_product_id),
        event_type=PRICE_DROP_EVENT_TYPE,
        idempotency_key=(
            f"supplier-price-drop:{observation.supplier_product_id}:"
            f"observation:{observation.id}"
        ),
        payload_json={
            "version": 1,
            "supplier_product_id": observation.supplier_product_id,
            "observation_id": observation.id,
            "baseline_price": f"{baseline_price:.2f}",
            "current_price": f"{current_price:.2f}",
            "drop_percent": str(drop_percent),
            "currency": observation.currency or "KZT",
            "baseline_sample_size": len(prices),
            "observed_at": observation.observed_at.isoformat(),
            **context,
        },
    )
    session.add(event)
    session.flush()
    return PriceDropAlert(
        event_id=event.id,
        supplier_product_id=observation.supplier_product_id,
        baseline_price=baseline_price,
        current_price=current_price,
        drop_percent=drop_percent,
        sample_size=len(prices),
    )
