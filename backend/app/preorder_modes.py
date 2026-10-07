"""Ownership of explicitly requested preorders, shared by the existing workers."""
from datetime import timedelta
from decimal import Decimal
from sqlalchemy import select, or_
from .product_test_models import ProductTestItem
from .fast_dumping_models import FastDumpingPolicy, FastDumpingJob, FastDumpingState
from .dumping_models import DumpingPolicy

PREFIX = "manual-preorder:"


def preorder_item(db, product):
    return db.scalar(select(ProductTestItem).where(
        ProductTestItem.workspace_id == product.workspace_id,
        ProductTestItem.product_id == product.id,
        ProductTestItem.input_reference.startswith(PREFIX),
    ).order_by(ProductTestItem.id.desc()).limit(1))


def pricing_locked(db, product):
    item = preorder_item(db, product)
    if item is None or (item.offers_json or {}).get("stock_mode"):
        return False
    from .dumping_service import physical_stock_count
    return physical_stock_count(db, product_id=product.id) <= 0 and not (item.offers_json or {}).get("dump_enabled", False)


def pause_pricing(db, product, item):
    from .fast_dumping_service import cancel_active_job
    policy = db.scalar(select(FastDumpingPolicy).where(
        FastDumpingPolicy.workspace_id == product.workspace_id,
        FastDumpingPolicy.product_id == product.id).with_for_update())
    details = dict(item.offers_json or {})
    if policy is not None:
        details.setdefault("resume_fast_on_stock", bool(policy.enabled))
        policy.enabled = False
        state = db.scalar(select(FastDumpingState).where(
            FastDumpingState.workspace_id == product.workspace_id,
            FastDumpingState.product_id == product.id).with_for_update())
        if state is not None:
            active = db.get(FastDumpingJob, state.active_job_id) if state.active_job_id else None
            # Already sent mutations must still be verified; never revoke their lease.
            if active is None or active.status in {"queued_scan", "leased_scan", "queued_apply"}:
                cancel_active_job(db, state=state, reason="Карточка переведена в предзаказ с заданной ценой")
            state.next_scan_at = None
            state.status = "paused"
            state.status_reason = "Предзаказ: заданная цена. Демпинг выключен."
    classic = db.scalar(select(DumpingPolicy).where(
        DumpingPolicy.workspace_id == product.workspace_id,
        DumpingPolicy.product_id == product.id).with_for_update())
    if classic is not None:
        classic.enabled = False
    item.offers_json = details


def configure_rocket(db, policy, enabled):
    if not enabled:
        policy.pricing_mode = "manual"
        policy.automation_config = None
        return
    from .full_automation_api import SNAPSHOT_FIELDS
    config = dict(policy.automation_config or {})
    if policy.pricing_mode != "automation":
        config["previous"] = {key: str(getattr(policy, key)) if isinstance(getattr(policy, key), Decimal)
                              else getattr(policy, key) for key in SNAPSHOT_FIELDS}
        config["classic_enabled"] = False
    config.update(minimum_profit_kzt=str(policy.minimum_profit_kzt), monitor_seconds=120,
                  premium_per_day_kzt=500, premium_cap_kzt=2000,
                  delivery_advantage_days=4, maximum_price_kzt=None, observation_hours=12)
    policy.pricing_mode = "automation"
    policy.automation_config = config


def activate_preorder_pricing(db, product, item):
    details = item.offers_json or {}
    if not details.get("dump_enabled"):
        pause_pricing(db, product, item)
        item.fast_dumping_policy_id = None
        return
    from .product_supplier_binding_api import attach_manual_supplier_binding, ManualSupplierBindingCreate
    from .fast_dumping_service import ensure_state, queue_scan, utcnow
    binding = attach_manual_supplier_binding(product.id, ManualSupplierBindingCreate(
        url=details["ozon_url"], is_primary=True), db, commit=False)
    item.offers_json = {**details, "monitor_binding_id": binding.binding_id}
    policy = db.scalar(select(FastDumpingPolicy).where(
        FastDumpingPolicy.workspace_id == product.workspace_id,
        FastDumpingPolicy.product_id == product.id).with_for_update())
    if policy is None:
        policy = FastDumpingPolicy(workspace_id=product.workspace_id, product_id=product.id,
            minimum_profit_kzt=Decimal(details.get("minimum_profit_kzt", 1000)))
        db.add(policy)
        db.flush()
    policy.enabled = True
    policy.minimum_profit_kzt = Decimal(details.get("minimum_profit_kzt", 1000))
    policy.city_id, policy.zone_id = item.city_id, item.zone_id
    configure_rocket(db, policy, bool(details.get("rocket_enabled")))
    state = ensure_state(db, policy=policy, workspace_id=product.workspace_id)
    state.automatic_writes_paused = False
    state.pause_reason = None
    state.state_version += 1
    state.next_scan_at = utcnow()
    state.automation_json = None
    item.fast_dumping_policy_id = policy.id
    queue_scan(db, policy=policy, workspace_id=product.workspace_id, reason="preorder_monitoring_enabled")


def monitored_preorder_source(db, product, fallback):
    item = preorder_item(db, product)
    if item is None or (item.offers_json or {}).get("stock_mode"):
        return fallback
    details = item.offers_json or {}
    if not details.get("dump_enabled"):
        return None
    from .suppliers import ProductBinding
    from .monitoring import SupplierOfferState, MonitorTarget
    from .dumping_service import DumpingCostSource
    from .fast_dumping_service import utcnow, _aware
    row = db.execute(select(SupplierOfferState, MonitorTarget).join(
        ProductBinding, ProductBinding.supplier_product_id == SupplierOfferState.supplier_product_id
    ).join(MonitorTarget, MonitorTarget.product_binding_id == ProductBinding.id).where(
        ProductBinding.id == details.get("monitor_binding_id"),
        ProductBinding.workspace_id == product.workspace_id,
        ProductBinding.product_id == product.id,
        ProductBinding.status.in_(("active", "confirmed", "degraded")),
        SupplierOfferState.workspace_id == product.workspace_id,
        MonitorTarget.workspace_id == product.workspace_id,
    )).first()
    if row is None:
        return None
    snapshot, target = row
    checked = _aware(snapshot.last_checked_at)
    if (target.status not in {"active", "degraded"} or snapshot.price is None or snapshot.price <= 0 or snapshot.available is not True
            or snapshot.delivery_days is None or snapshot.delivery_days < 0
            or checked is None or utcnow() - checked > timedelta(seconds=max(900, target.interval_seconds * 2))):
        return None
    return DumpingCostSource(kind="supplier", name="Ozon", unit_cost_kzt=Decimal(snapshot.price),
                             delivery_days=int(snapshot.delivery_days))


def reconcile_preorder_policies(db, workspace):
    # One bounded indexed join; no historical job/offer JSON scans on every claim.
    rows = db.execute(select(ProductTestItem, FastDumpingPolicy).outerjoin(
        FastDumpingPolicy, FastDumpingPolicy.product_id == ProductTestItem.product_id
    ).outerjoin(DumpingPolicy, (DumpingPolicy.product_id == ProductTestItem.product_id)
                & (DumpingPolicy.workspace_id == ProductTestItem.workspace_id))
        .where(ProductTestItem.workspace_id == workspace,
        or_(FastDumpingPolicy.enabled.is_(True), DumpingPolicy.enabled.is_(True)),
        ProductTestItem.input_reference.startswith(PREFIX),
        ProductTestItem.offers_json["dump_enabled"].as_boolean().is_not(True),
        ProductTestItem.offers_json["stock_mode"].as_boolean().is_not(True))).all()
    from .models import Product
    for item, policy in rows:
        product = db.get(Product, item.product_id)
        if product and pricing_locked(db, product):
            pause_pricing(db, product, item)


def stock_arrived(db, product_id):
    from .models import Product
    from .dumping_service import physical_stock_count, _latest_feed_for_update, set_feed_offer_availability, _sku_candidates
    from .fast_dumping_service import ensure_state, utcnow
    product = db.get(Product, product_id)
    if product is None:
        return False
    item = preorder_item(db, product)
    if item is None or (item.offers_json or {}).get("stock_mode"):
        return False
    stock = physical_stock_count(db, product_id=product_id)
    if stock <= 0:
        return False
    item.offers_json = {**(item.offers_json or {}), "stock_mode": True}
    item.status = "stock_trading"
    # Preserve the sale price while immediately resetting fulfillment in XML.
    feed = _latest_feed_for_update(db)
    if feed:
        product = db.scalar(select(Product).where(Product.id == product_id,
            Product.workspace_id == product.workspace_id).with_for_update().execution_options(populate_existing=True))
    if feed and product and product.sale_enabled:
        for field in ("source_xml", "generated_xml"):
            xml = getattr(feed, field) or feed.source_xml
            try:
                setattr(feed, field, set_feed_offer_availability(xml, sku_candidates=_sku_candidates(product),
                    available=True, stock_count=stock, preorder_days=0))
            except ValueError as exc:
                if "Товар не найден" not in str(exc):
                    raise
        feed.generated_at = utcnow()
    policy = db.scalar(select(FastDumpingPolicy).where(
        FastDumpingPolicy.workspace_id == product.workspace_id,
        FastDumpingPolicy.product_id == product_id))
    if policy and ((item.offers_json or {}).get("dump_enabled") or (item.offers_json or {}).get("resume_fast_on_stock")):
        policy.enabled = True
        state = ensure_state(db, policy=policy, workspace_id=product.workspace_id)
        state.source_kind = "supplier"  # Forces the established priority stock transition.
        state.next_scan_at = utcnow()
    return True
