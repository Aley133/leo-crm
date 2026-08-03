from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING
from xml.etree import ElementTree

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .browser_agent_dispatch import queue_browser_target_now
from .commerce.profit_calculator import KASPI_COMMISSION_RATE, TAX_RATE, kaspi_logistics_per_unit
from .dumping_models import DumpingPolicy, DumpingRun, KaspiXmlFeed
from .inventory_models import InventoryBatch, InventoryBatchType
from .models import Product
from .monitoring import MonitorStatus, MonitorTarget, SupplierOfferState
from .supplier_identity import canonical_supplier_product_identity
from .suppliers import ProductBinding, Supplier, SupplierProduct
from .workspace_context import current_workspace_id
from .workspace_models import Workspace
from .product_inventory_group import (
    inventory_owner_ids_for_products,
    inventory_owner_product_id,
)


MONEY = Decimal("0.01")
ONE = Decimal("1")


def workspace_feed_url(db: Session) -> str:
    slug = db.scalar(
        select(Workspace.slug).where(Workspace.id == current_workspace_id())
    )
    return (
        f"/feeds/kaspi/{slug}/catalog.xml"
        if slug
        else "/feeds/kaspi/catalog.xml"
    )


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
    stock_count: int
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


def _inventory_sources(
    db: Session,
    product_ids: set[int],
    *,
    owner_by_product: dict[int, int] | None = None,
) -> dict[int, DumpingCostSource]:
    if not product_ids:
        return {}
    owner_by_product = owner_by_product or inventory_owner_ids_for_products(
        db,
        product_ids,
    )
    owner_ids = set(owner_by_product.values())
    batches = db.scalars(
        select(InventoryBatch)
        .where(
            InventoryBatch.product_id.in_(owner_ids),
            InventoryBatch.batch_type == InventoryBatchType.PURCHASE.value,
            InventoryBatch.is_received.is_(True),
            InventoryBatch.quantity_remaining > 0,
        )
        .order_by(
            InventoryBatch.product_id,
            InventoryBatch.received_at,
            InventoryBatch.id,
        )
    ).all()
    result_by_owner: dict[int, DumpingCostSource] = {}
    for batch in batches:
        owner_id = int(batch.product_id)
        result_by_owner.setdefault(
            owner_id,
            DumpingCostSource(
                kind="inventory",
                name=batch.source_name or "Склад FIFO",
                unit_cost_kzt=Decimal(batch.unit_cost),
                delivery_days=0,
            ),
        )
    return {
        product_id: result_by_owner[owner_id]
        for product_id, owner_id in owner_by_product.items()
        if owner_id in result_by_owner
    }


def _supplier_sources(
    db: Session,
    product_ids: set[int],
    *,
    owner_by_product: dict[int, int] | None = None,
) -> dict[int, DumpingCostSource]:
    if not product_ids:
        return {}
    owner_by_product = owner_by_product or inventory_owner_ids_for_products(
        db,
        product_ids,
    )
    owner_ids = set(owner_by_product.values())
    group_products = db.scalars(
        select(Product).where(
            or_(
                Product.id.in_(owner_ids),
                Product.inventory_owner_product_id.in_(owner_ids),
            )
        )
    ).all()
    owner_by_group_product = {
        int(product.id): (
            int(product.inventory_owner_product_id)
            if product.inventory_owner_product_id is not None
            else int(product.id)
        )
        for product in group_products
    }
    rows = db.execute(
        select(ProductBinding, SupplierProduct, Supplier, SupplierOfferState)
        .join(SupplierProduct, SupplierProduct.id == ProductBinding.supplier_product_id)
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .join(Product, Product.id == ProductBinding.product_id)
        .outerjoin(
            SupplierOfferState,
            SupplierOfferState.supplier_product_id == SupplierProduct.id,
        )
        .where(
            or_(
                Product.id.in_(owner_ids),
                Product.inventory_owner_product_id.in_(owner_ids),
            ),
            ProductBinding.status.in_(("active", "confirmed", "degraded")),
        )
        .order_by(
            ProductBinding.product_id,
            ProductBinding.is_primary.desc(),
            ProductBinding.priority,
            SupplierOfferState.observed_at.desc().nullslast(),
        )
    ).all()
    grouped_rows: dict[
        tuple[int, int, str],
        list[
            tuple[
                ProductBinding,
                SupplierProduct,
                Supplier,
                SupplierOfferState | None,
            ]
        ],
    ] = {}
    for binding, supplier_product, supplier, state in rows:
        owner_id = owner_by_group_product[int(binding.product_id)]
        identity = canonical_supplier_product_identity(
            supplier_code=supplier.code,
            external_id=supplier_product.external_id,
            url=supplier_product.url,
        )
        grouped_rows.setdefault((owner_id, supplier.id, identity), []).append(
            (binding, supplier_product, supplier, state)
        )

    def freshness(
        row: tuple[
            ProductBinding,
            SupplierProduct,
            Supplier,
            SupplierOfferState | None,
        ],
    ) -> tuple[datetime, bool, bool, int, int]:
        binding, supplier_product, _supplier, state = row
        checked_at = (
            state.last_checked_at
            if state is not None
            else supplier_product.last_checked_at
        )
        if checked_at is None:
            checked_at = supplier_product.created_at
        if checked_at is None:
            checked_at = datetime.min.replace(tzinfo=UTC)
        elif checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=UTC)
        else:
            checked_at = checked_at.astimezone(UTC)
        return (
            checked_at,
            state is not None,
            binding.is_primary,
            -binding.priority,
            binding.id,
        )

    result_by_owner: dict[int, DumpingCostSource] = {}
    for (owner_id, _supplier_id, _identity), duplicate_rows in grouped_rows.items():
        if owner_id in result_by_owner:
            continue
        _binding, supplier_product, supplier, state = max(
            duplicate_rows,
            key=freshness,
        )
        if state is not None and state.price is not None and state.available is not False:
            result_by_owner[owner_id] = DumpingCostSource(
                kind="supplier",
                name=supplier.name,
                unit_cost_kzt=Decimal(state.price),
                delivery_days=max(int(state.delivery_days or 0), 0),
            )
        elif (
            state is None
            and supplier_product.current_price is not None
            and supplier_product.in_stock is not False
        ):
            result_by_owner[owner_id] = DumpingCostSource(
                kind="supplier",
                name=supplier.name,
                unit_cost_kzt=Decimal(supplier_product.current_price),
                delivery_days=max(int(supplier_product.delivery_days or 0), 0),
            )
    return {
        product_id: result_by_owner[owner_id]
        for product_id, owner_id in owner_by_product.items()
        if owner_id in result_by_owner
    }


def resolve_cost_sources(
    db: Session,
    *,
    product_ids: set[int],
    owner_by_product: dict[int, int] | None = None,
) -> dict[int, DumpingCostSource | None]:
    """Resolve pricing inputs for a page in a bounded number of SQL reads."""
    normalized = {int(product_id) for product_id in product_ids}
    if not normalized:
        return {}
    resolved_owners = owner_by_product or inventory_owner_ids_for_products(
        db,
        normalized,
    )
    inventory = _inventory_sources(
        db,
        normalized,
        owner_by_product=resolved_owners,
    )
    missing = normalized.difference(inventory)
    suppliers = _supplier_sources(
        db,
        missing,
        owner_by_product={
            product_id: resolved_owners[product_id]
            for product_id in missing
            if product_id in resolved_owners
        },
    )
    return {
        product_id: inventory.get(product_id) or suppliers.get(product_id)
        for product_id in normalized
    }


def resolve_cost_source(db: Session, *, product_id: int, inventory_first: bool = True) -> DumpingCostSource | None:
    # Physical stock is authoritative regardless of a legacy policy toggle.
    # Selling a supplier preorder while warehouse units are still available
    # would make XML quantity and FIFO accounting disagree.
    return resolve_cost_sources(db, product_ids={product_id}).get(product_id)


def physical_stock_counts(
    db: Session,
    *,
    product_ids: set[int],
    owner_by_product: dict[int, int] | None = None,
) -> dict[int, int]:
    """Return authoritative on-hand stock for several products in one query."""
    normalized = {int(product_id) for product_id in product_ids}
    if not normalized:
        return {}
    owner_by_product = owner_by_product or inventory_owner_ids_for_products(
        db,
        normalized,
    )
    owner_ids = set(owner_by_product.values())
    rows = db.execute(
        select(
            InventoryBatch.product_id,
            func.coalesce(func.sum(InventoryBatch.quantity_remaining), 0),
        )
        .where(
            InventoryBatch.product_id.in_(owner_ids),
            InventoryBatch.batch_type == InventoryBatchType.PURCHASE.value,
            InventoryBatch.is_received.is_(True),
        )
        .group_by(InventoryBatch.product_id)
    ).all()
    counted = {int(product_id): int(quantity or 0) for product_id, quantity in rows}
    return {
        product_id: counted.get(owner_by_product.get(product_id, product_id), 0)
        for product_id in normalized
    }


def physical_stock_count(db: Session, *, product_id: int) -> int:
    """Return sellable on-hand units from received purchase batches only."""
    return physical_stock_counts(db, product_ids={product_id}).get(product_id, 0)


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
        stock_count=physical_stock_count(db, product_id=product.id),
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


def _qualified_tag(parent: ElementTree.Element, local_name: str) -> str:
    tag = str(parent.tag)
    if tag.startswith("{") and "}" in tag:
        namespace = tag.split("}", 1)[0] + "}"
        return f"{namespace}{local_name}"
    return local_name


def _create_managed_offer(
    root: ElementTree.Element,
    *,
    product: Product,
    city_id: str,
) -> ElementTree.Element:
    """Create the smallest valid managed offer when Seller XML omitted it.

    Product identity and descriptive fields come from the registry; price and
    availability are filled by the same dumping decision that requested the
    recovery. This keeps an available supplier preorder from being confused
    with a missing procurement source.
    """

    offers = next(
        (node for node in root.iter() if _local_name(node.tag) == "offers"),
        root,
    )
    offer = ElementTree.SubElement(
        offers,
        _qualified_tag(offers, "offer"),
        {"sku": (product.merchant_sku or product.kaspi_product_id).strip()},
    )
    model = ElementTree.SubElement(offer, _qualified_tag(offer, "model"))
    model.text = product.name
    if product.brand:
        brand = ElementTree.SubElement(offer, _qualified_tag(offer, "brand"))
        brand.text = product.brand
    cityprices = ElementTree.SubElement(
        offer,
        _qualified_tag(offer, "cityprices"),
    )
    ElementTree.SubElement(
        cityprices,
        _qualified_tag(cityprices, "cityprice"),
        {"cityId": city_id},
    )
    ElementTree.SubElement(
        offer,
        _qualified_tag(offer, "availability"),
    )
    return offer


def update_feed_xml(
    xml_text: str,
    *,
    sku_candidates: set[str],
    price_kzt: Decimal,
    preorder_days: int,
    stock_count: int,
    product: Product | None = None,
    city_id: str = "750000000",
) -> str:
    root = ElementTree.fromstring(xml_text.encode("utf-8"))
    offer = _matching_offer(root, {value for value in sku_candidates if value})
    if offer is None:
        if product is None:
            raise ValueError("Товар не найден в сохранённом XML по SKU/Kaspi ID")
        offer = _create_managed_offer(
            root,
            product=product,
            city_id=city_id,
        )

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
    availability.set("stockCount", str(max(int(stock_count), 0)))

    return ElementTree.tostring(root, encoding="unicode", xml_declaration=True)


def set_feed_offer_availability(
    xml_text: str,
    *,
    sku_candidates: set[str],
    available: bool,
    stock_count: int | None = None,
    preorder_days: int | None = None,
) -> str:
    """Synchronize one offer without deleting its recoverable XML identity."""
    root = ElementTree.fromstring(xml_text.encode("utf-8"))
    offer = _matching_offer(root, {value for value in sku_candidates if value})
    if offer is None:
        raise ValueError("Товар не найден в сохранённом XML по SKU/Kaspi ID")

    availability = next(
        (node for node in offer.iter() if _local_name(node.tag) == "availability"),
        None,
    )
    if availability is None:
        availability = ElementTree.SubElement(offer, "availability")
    availability.set("available", "yes" if available else "no")
    if stock_count is not None:
        availability.set("stockCount", str(max(int(stock_count), 0)))
    if not available:
        availability.set("preOrder", "0")
        availability.set("stockCount", "0")
    elif preorder_days is not None:
        availability.set("preOrder", str(max(int(preorder_days), 0)))
    return ElementTree.tostring(root, encoding="unicode", xml_declaration=True)


def feed_offer_is_expected_active(
    xml_text: str,
    *,
    sku_candidates: set[str],
) -> bool | None:
    """Return whether our XML currently expects Kaspi to show the offer."""
    root = ElementTree.fromstring(xml_text.encode("utf-8"))
    offer = _matching_offer(root, {value for value in sku_candidates if value})
    if offer is None:
        return None
    availability = next(
        (node for node in offer.iter() if _local_name(node.tag) == "availability"),
        None,
    )
    if availability is None:
        return True
    return str(availability.attrib.get("available") or "yes").strip().casefold() != "no"


def _latest_feed_for_update(db: Session) -> KaspiXmlFeed | None:
    return db.scalar(
        select(KaspiXmlFeed)
        .where(
            KaspiXmlFeed.workspace_id == current_workspace_id(),
            KaspiXmlFeed.active.is_(True),
        )
        .order_by(KaspiXmlFeed.id.desc())
        .with_for_update()
        .limit(1)
    )


def _sku_candidates(product: Product) -> set[str]:
    return {product.merchant_sku or "", product.kaspi_product_id}


def _latest_run(db: Session, *, product_id: int) -> DumpingRun | None:
    return db.scalar(
        select(DumpingRun)
        .where(DumpingRun.product_id == product_id)
        .order_by(DumpingRun.id.desc())
        .limit(1)
    )


def _seller_removal_is_latched(
    db: Session,
    *,
    product_id: int,
    policy: DumpingPolicy | None,
) -> bool:
    if policy is None or policy.enabled:
        return False
    latest = _latest_run(db, product_id=product_id)
    return latest is not None and latest.status == "suspended_seller_removed"


def _queue_fresh_supplier_snapshot(
    db: Session,
    *,
    product_id: int,
) -> tuple[int | None, int | None]:
    owner_id = inventory_owner_product_id(db, product_id)
    rows = db.execute(
        select(MonitorTarget.id, Supplier.code, SupplierProduct.id)
        .join(ProductBinding, ProductBinding.id == MonitorTarget.product_binding_id)
        .join(SupplierProduct, SupplierProduct.id == ProductBinding.supplier_product_id)
        .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
        .join(Product, Product.id == ProductBinding.product_id)
        .where(
            or_(
                Product.id == owner_id,
                Product.inventory_owner_product_id == owner_id,
            ),
            ProductBinding.status.in_(("active", "confirmed", "degraded")),
            MonitorTarget.status == MonitorStatus.ACTIVE.value,
            Supplier.is_active.is_(True),
        )
        .order_by(
            ProductBinding.is_primary.desc(),
            ProductBinding.priority,
            MonitorTarget.id,
        )
    ).all()
    for target_id, supplier_code, supplier_product_id in rows:
        queued = queue_browser_target_now(
            db,
            target_id=int(target_id),
            supplier_code=str(supplier_code),
        )
        if queued.job_id is not None:
            return int(queued.job_id), int(supplier_product_id)
    return None, None


def _mark_waiting_runs(
    db: Session,
    *,
    product_id: int,
    status: str,
) -> None:
    waiting = db.scalars(
        select(DumpingRun)
        .where(
            DumpingRun.product_id == product_id,
            DumpingRun.status.in_((
                "awaiting_supplier_refresh",
                "supplier_refresh_ready",
            )),
        )
        .with_for_update()
    ).all()
    for run in waiting:
        run.status = status


def sync_product_inventory_to_feed(
    db: Session,
    *,
    product_id: int,
    reason: str,
) -> dict[str, int | str | None]:
    """Atomically project authoritative FIFO stock into the active Kaspi XML.

    Stock sales never wait for the periodic dumping cycle. When the last unit
    is reserved, the offer is closed first and a fresh supplier observation is
    queued. Only that fresh observation may reopen it as a preorder.
    """
    product = db.get(Product, product_id)
    if product is None:
        return {"stock_count": 0, "xml_state": "product_missing", "supplier_job_id": None}
    feed = _latest_feed_for_update(db)
    if feed is None:
        return {"stock_count": 0, "xml_state": "feed_missing", "supplier_job_id": None}

    stock_count = physical_stock_count(db, product_id=product_id)
    policy = db.scalar(
        select(DumpingPolicy)
        .where(DumpingPolicy.product_id == product_id)
        .with_for_update()
    )
    seller_removal_latched = _seller_removal_is_latched(
        db,
        product_id=product_id,
        policy=policy,
    )
    try:
        feed.generated_xml = set_feed_offer_availability(
            feed.generated_xml or feed.source_xml,
            sku_candidates=_sku_candidates(product),
            available=stock_count > 0 and not seller_removal_latched,
            stock_count=stock_count,
            preorder_days=0,
        )
    except ValueError as exc:
        if str(exc) == "Товар не найден в сохранённом XML по SKU/Kaspi ID":
            return {
                "stock_count": stock_count,
                "xml_state": "offer_absent",
                "supplier_job_id": None,
            }
        raise
    feed.generated_at = func.now()

    if seller_removal_latched:
        return {
            "stock_count": stock_count,
            "xml_state": "seller_removed",
            "supplier_job_id": None,
        }
    if stock_count > 0:
        _mark_waiting_runs(
            db,
            product_id=product_id,
            status="inventory_restocked",
        )
        return {
            "stock_count": stock_count,
            "xml_state": "stock",
            "supplier_job_id": None,
        }
    if policy is None or not policy.enabled or not policy.auto_publish_xml:
        return {
            "stock_count": 0,
            "xml_state": "closed_without_preorder_policy",
            "supplier_job_id": None,
        }

    supplier_job_id, supplier_product_id = _queue_fresh_supplier_snapshot(
        db,
        product_id=product_id,
    )
    latest = _latest_run(db, product_id=product_id)
    if latest is None or latest.status != "awaiting_supplier_refresh":
        db.add(
            DumpingRun(
                product_id=product_id,
                dumping_policy_id=policy.id,
                status="awaiting_supplier_refresh",
                published=True,
                preorder_days=0,
                explanation_json={
                    "reason": reason,
                    "business_state": "stock_depleted",
                    "stock_count": 0,
                    "xml_availability": "no",
                    "supplier_refresh_job_id": supplier_job_id,
                    "supplier_product_id": supplier_product_id,
                    "automatic_recovery": supplier_job_id is not None,
                },
            )
        )
        db.flush()
    return {
        "stock_count": 0,
        "xml_state": "awaiting_supplier_refresh",
        "supplier_job_id": supplier_job_id,
    }


def close_untracked_order_offer(
    db: Session,
    *,
    sku_candidates: set[str],
) -> bool:
    """Fail closed when an active order cannot yet be linked to a CRM product."""
    feed = _latest_feed_for_update(db)
    if feed is None:
        return False
    try:
        feed.generated_xml = set_feed_offer_availability(
            feed.generated_xml or feed.source_xml,
            sku_candidates=sku_candidates,
            available=False,
            stock_count=0,
        )
    except ValueError as exc:
        if str(exc) == "Товар не найден в сохранённом XML по SKU/Kaspi ID":
            return False
        raise
    feed.generated_at = func.now()
    return True


def suspend_product_removed_from_seller(
    db: Session,
    *,
    product: Product,
    policy: DumpingPolicy,
) -> DumpingRun:
    """Latch a manual Kaspi Seller removal until the owner re-enables it."""
    feed = _latest_feed_for_update(db)
    xml_availability = "no"
    if feed is not None:
        try:
            feed.generated_xml = set_feed_offer_availability(
                feed.generated_xml or feed.source_xml,
                sku_candidates=_sku_candidates(product),
                available=False,
                stock_count=0,
            )
        except ValueError as exc:
            if str(exc) != "Товар не найден в сохранённом XML по SKU/Kaspi ID":
                raise
            xml_availability = "offer_absent"
        feed.generated_at = func.now()

    policy.enabled = False
    queued_jobs = db.scalars(
        select(DumpingRun)
        .where(
            DumpingRun.product_id == product.id,
            DumpingRun.status == "queued_local",
        )
        .with_for_update()
    ).all()
    for job in queued_jobs:
        job.status = "failed_local"
        job.explanation_json = {
            **(job.explanation_json or {}),
            "stage": "cancelled_seller_removed",
            "error_code": "seller_offer_removed",
            "error_message": "Карточка снята владельцем в Kaspi Seller",
        }

    latest = _latest_run(db, product_id=product.id)
    if latest is not None and latest.status == "suspended_seller_removed":
        return latest
    run = DumpingRun(
        product_id=product.id,
        dumping_policy_id=policy.id,
        status="suspended_seller_removed",
        published=True,
        explanation_json={
            "reason": "own_offer_absent_while_xml_expected_active",
            "business_state": "owner_removed_from_kaspi_seller",
            "xml_availability": xml_availability,
            "automatic_recovery": False,
            "resume_action": "explicitly_enable_dumping_policy",
        },
    )
    db.add(run)
    db.flush()
    return run


def suspend_product_without_cost_source(
    db: Session,
    *,
    product: Product,
    policy: DumpingPolicy,
    reason: str = "no_available_procurement_source",
) -> DumpingRun:
    """Stop XML sales while preserving the policy for automatic recovery.

    The policy remains enabled so supplier monitoring can resume dumping as
    soon as any bound source becomes available again. Only executable Kaspi
    competitor jobs are cancelled; a currently leased job may finish, but its
    decision will still be rejected because there is no cost source.
    """
    feed = _latest_feed_for_update(db)
    if feed is None:
        raise ValueError("Сначала импортируйте полный Kaspi XML в разделе Товары")

    xml_availability = "no"
    try:
        feed.generated_xml = set_feed_offer_availability(
            feed.generated_xml or feed.source_xml,
            sku_candidates=_sku_candidates(product),
            available=False,
            stock_count=0,
        )
    except ValueError as exc:
        if str(exc) != "Товар не найден в сохранённом XML по SKU/Kaspi ID":
            raise
        # Absence from the feed is already a safe non-sellable state.
        xml_availability = "offer_absent"
    feed.active = True
    feed.generated_at = func.now()
    _mark_waiting_runs(
        db,
        product_id=product.id,
        status="supplier_refresh_unavailable",
    )

    queued_jobs = db.scalars(
        select(DumpingRun)
        .where(
            DumpingRun.product_id == product.id,
            DumpingRun.status == "queued_local",
        )
        .with_for_update()
    ).all()
    for job in queued_jobs:
        job.status = "failed_local"
        job.explanation_json = {
            **(job.explanation_json or {}),
            "stage": "cancelled_no_source",
            "error_code": "no_available_procurement_source",
            "error_message": "Нет доступного источника закупки; товар закрыт в XML",
        }

    latest = db.scalar(
        select(DumpingRun)
        .where(DumpingRun.product_id == product.id)
        .order_by(DumpingRun.id.desc())
        .limit(1)
    )
    if latest is not None and latest.status == "suspended_no_source":
        return latest

    run = DumpingRun(
        product_id=product.id,
        dumping_policy_id=policy.id,
        status="suspended_no_source",
        published=True,
        explanation_json={
            "reason": reason,
            "business_state": "out_of_stock",
            "xml_availability": xml_availability,
            "automatic_recovery": True,
        },
    )
    db.add(run)
    db.flush()
    return run


def publish_decision(db: Session, *, product: Product, policy: DumpingPolicy, decision: DumpingDecision) -> DumpingRun:
    feed = _latest_feed_for_update(db)
    if feed is None:
        raise ValueError("Сначала импортируйте полный Kaspi XML в разделе Товары")

    generated = update_feed_xml(
        feed.generated_xml or feed.source_xml,
        sku_candidates=_sku_candidates(product),
        price_kzt=decision.target_price_kzt,
        preorder_days=decision.preorder_days,
        stock_count=decision.stock_count,
        product=product,
        city_id=policy.city_id,
    )
    feed.generated_xml = generated
    feed.active = True
    feed.generated_at = func.now()
    _mark_waiting_runs(
        db,
        product_id=product.id,
        status="supplier_refresh_applied",
    )

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
            "stock_count": decision.stock_count,
            "logistics_kzt": str(kaspi_logistics_per_unit(decision.target_price_kzt)),
            "feed_url": workspace_feed_url(db),
            "xml_offer_recovered": feed_offer_is_expected_active(
                feed.source_xml,
                sku_candidates=_sku_candidates(product),
            ) is None,
        },
    )
    db.add(run)
    db.flush()
    return run
