from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from xml.etree import ElementTree

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .commerce.profit_calculator import KASPI_COMMISSION_RATE, TAX_RATE, kaspi_logistics_per_unit
from .dumping_models import DumpingPolicy, DumpingRun, KaspiXmlFeed
from .inventory_models import InventoryBatch
from .models import Product
from .monitoring import SupplierOfferState
from .suppliers import ProductBinding, Supplier, SupplierProduct


MONEY = Decimal("0.01")
ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class DumpingCostSource:
    kind: str
    name: str
    unit_cost_kzt: Decimal
    delivery_days: int


@dataclass(frozen=True, slots=True)
class DumpingDecision:
    product_id: int
    source: DumpingCostSource
    safe_floor_kzt: Decimal
    preorder_days: int
    competitor_price_kzt: Decimal | None
    own_price_kzt: Decimal | None
    target_price_kzt: Decimal
    status: str


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY)


def calculate_safe_floor(*, unit_cost_kzt: Decimal, minimum_profit_kzt: Decimal) -> Decimal:
    """Return the lowest sale price that preserves the requested net profit.

    Kaspi commission, tax and the owner's fixed logistics tariff are included.
    The tariff depends on the resulting sale price, so each tariff band is
    checked independently and the first valid candidate becomes authoritative.
    """

    cost = Decimal(unit_cost_kzt)
    minimum_profit = Decimal(minimum_profit_kzt)
    retained_rate = ONE - KASPI_COMMISSION_RATE - TAX_RATE
    bands: tuple[tuple[Decimal, Decimal | None, Decimal], ...] = (
        (Decimal("0"), Decimal("1000"), Decimal("57")),
        (Decimal("1000"), Decimal("3000"), Decimal("173")),
        (Decimal("3000"), Decimal("5000"), Decimal("231")),
        (Decimal("5000"), Decimal("10000"), Decimal("927")),
        (Decimal("10000"), None, Decimal("1507")),
    )
    for lower, upper, logistics in bands:
        raw = (cost + minimum_profit + logistics) / retained_rate
        candidate = max(lower, raw.to_integral_value(rounding=ROUND_CEILING))
        if upper is None or candidate < upper:
            return _money(candidate)
    raise RuntimeError("Unable to calculate dumping floor")


def _inventory_source(db: Session, product_id: int) -> DumpingCostSource | None:
    batch = db.scalar(
        select(InventoryBatch)
        .where(
            InventoryBatch.product_id == product_id,
            InventoryBatch.quantity_remaining > 0,
        )
        .order_by(InventoryBatch.received_at, InventoryBatch.id)
        .limit(1)
    )
    if batch is None:
        return None
    return DumpingCostSource(
        kind="inventory",
        name=batch.source_name or "Склад FIFO",
        unit_cost_kzt=Decimal(batch.unit_cost),
        delivery_days=0,
    )


def _supplier_source(db: Session, product_id: int) -> DumpingCostSource | None:
    rows = db.execute(
        select(ProductBinding, SupplierProduct, Supplier, SupplierOfferState)
        .join(SupplierProduct, SupplierProduct.id == ProductBinding.supplier_product_id)
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .outerjoin(
            SupplierOfferState,
            SupplierOfferState.supplier_product_id == SupplierProduct.id,
        )
        .where(
            ProductBinding.product_id == product_id,
            ProductBinding.status.in_(("active", "confirmed", "degraded")),
        )
        .order_by(
            ProductBinding.is_primary.desc(),
            ProductBinding.priority,
            SupplierOfferState.observed_at.desc().nullslast(),
        )
    ).all()
    for _binding, supplier_product, supplier, state in rows:
        if state is not None and state.price is not None and state.available is not False:
            return DumpingCostSource(
                kind="supplier",
                name=supplier.name,
                unit_cost_kzt=Decimal(state.price),
                delivery_days=max(int(state.delivery_days or 0), 0),
            )
        if supplier_product.current_price is not None and supplier_product.in_stock is not False:
            return DumpingCostSource(
                kind="supplier",
                name=supplier.name,
                unit_cost_kzt=Decimal(supplier_product.current_price),
                delivery_days=max(int(supplier_product.delivery_days or 0), 0),
            )
    return None


def resolve_cost_source(db: Session, *, product_id: int, inventory_first: bool = True) -> DumpingCostSource | None:
    if inventory_first:
        return _inventory_source(db, product_id) or _supplier_source(db, product_id)
    return _supplier_source(db, product_id) or _inventory_source(db, product_id)


def decide_dumping_price(
    db: Session,
    *,
    product: Product,
    policy: DumpingPolicy,
    competitor_price_kzt: Decimal | None,
    own_price_kzt: Decimal | None,
) -> DumpingDecision:
    source = resolve_cost_source(db, product_id=product.id, inventory_first=policy.inventory_first)
    if source is None:
        raise ValueError("Нет доступной партии или актуального предложения поставщика")

    floor = calculate_safe_floor(
        unit_cost_kzt=source.unit_cost_kzt,
        minimum_profit_kzt=Decimal(policy.minimum_profit_kzt),
    )
    preorder_days = 0 if source.kind == "inventory" else source.delivery_days + int(policy.supplier_delivery_buffer_days)

    if competitor_price_kzt is None:
        target = floor
        status = "floor_only"
    else:
        undercut = max(Decimal("1"), Decimal(competitor_price_kzt) - Decimal(policy.undercut_step_kzt))
        target = max(floor, undercut)
        status = "ready" if undercut >= floor else "floor_limited"

    return DumpingDecision(
        product_id=product.id,
        source=source,
        safe_floor_kzt=floor,
        preorder_days=preorder_days,
        competitor_price_kzt=None if competitor_price_kzt is None else _money(Decimal(competitor_price_kzt)),
        own_price_kzt=None if own_price_kzt is None else _money(Decimal(own_price_kzt)),
        target_price_kzt=_money(target),
        status=status,
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _matching_offer(root: ElementTree.Element, sku_candidates: set[str]) -> ElementTree.Element | None:
    for element in root.iter():
        if _local_name(element.tag) != "offer":
            continue
        sku = (element.attrib.get("sku") or element.attrib.get("id") or "").strip()
        if sku in sku_candidates:
            return element
    return None


def update_feed_xml(
    xml_text: str,
    *,
    sku_candidates: set[str],
    price_kzt: Decimal,
    preorder_days: int,
) -> str:
    root = ElementTree.fromstring(xml_text.encode("utf-8"))
    offer = _matching_offer(root, {value for value in sku_candidates if value})
    if offer is None:
        raise ValueError("Товар не найден в сохранённом XML по SKU/Kaspi ID")

    price_nodes = [node for node in offer.iter() if _local_name(node.tag) == "cityprice"]
    if not price_nodes:
        raise ValueError("В XML-предложении отсутствуют cityprice")
    rendered_price = str(int(price_kzt)) if price_kzt == price_kzt.to_integral_value() else format(price_kzt, "f")
    for node in price_nodes:
        node.text = rendered_price

    availability = next((node for node in offer.iter() if _local_name(node.tag) == "availability"), None)
    if availability is None:
        availability = ElementTree.SubElement(offer, "availability")
    availability.set("available", "yes")
    availability.set("preOrder", str(max(int(preorder_days), 0)))

    return ElementTree.tostring(root, encoding="unicode", xml_declaration=True)


def publish_decision(db: Session, *, product: Product, policy: DumpingPolicy, decision: DumpingDecision) -> DumpingRun:
    feed = db.scalar(select(KaspiXmlFeed).order_by(KaspiXmlFeed.id.desc()).limit(1))
    if feed is None:
        raise ValueError("Сначала импортируйте полный Kaspi XML в разделе Товары")

    generated = update_feed_xml(
        feed.generated_xml or feed.source_xml,
        sku_candidates={product.merchant_sku or "", product.kaspi_product_id},
        price_kzt=decision.target_price_kzt,
        preorder_days=decision.preorder_days,
    )
    feed.generated_xml = generated
    feed.active = True
    feed.generated_at = func.now()

    run = DumpingRun(
        product_id=product.id,
        dumping_policy_id=policy.id,
        status=decision.status,
        source_kind=decision.source.kind,
        source_name=decision.source.name,
        source_cost_kzt=decision.source.unit_cost_kzt,
        source_delivery_days=decision.source.delivery_days,
        safe_floor_kzt=decision.safe_floor_kzt,
        own_price_kzt=decision.own_price_kzt,
        competitor_price_kzt=decision.competitor_price_kzt,
        target_price_kzt=decision.target_price_kzt,
        preorder_days=decision.preorder_days,
        published=True,
        explanation_json={
            "minimum_profit_kzt": str(policy.minimum_profit_kzt),
            "undercut_step_kzt": int(policy.undercut_step_kzt),
            "supplier_delivery_buffer_days": int(policy.supplier_delivery_buffer_days),
            "logistics_kzt": str(kaspi_logistics_per_unit(decision.target_price_kzt)),
            "feed_url": "/feeds/kaspi/catalog.xml",
        },
    )
    db.add(run)
    db.flush()
    return run
