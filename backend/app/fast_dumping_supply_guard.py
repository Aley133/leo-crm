"""Remove empty, unbound products from the price worker queue."""
from sqlalchemy import or_, select
from .models import Product
from .suppliers import ProductBinding
from .fast_dumping_models import FastDumpingJob, FastDumpingPolicy, FastDumpingState
from .product_inventory_group import inventory_owner_ids_for_products
from .dumping_service import physical_stock_counts

REASON = "Отключена: физический остаток 0, поставщик не привязан."
BOUND_STATUSES = ("active", "confirmed", "degraded")
PREORDER_PAUSE = "Предзаказ: заданная цена. Демпинг выключен."


def resume_stock_products(db, workspace, product_ids=None):
    """Reverse an automatic supply/preorder stop only after physical FIFO arrives."""
    from .fast_dumping_service import queue_scan, utcnow
    db.flush()
    query = select(FastDumpingPolicy, Product, FastDumpingState).join(
        Product, Product.id == FastDumpingPolicy.product_id
    ).join(FastDumpingState, FastDumpingState.policy_id == FastDumpingPolicy.id).where(
        FastDumpingPolicy.workspace_id == workspace, Product.workspace_id == workspace,
        FastDumpingState.workspace_id == workspace, FastDumpingPolicy.enabled.is_(False),
        FastDumpingState.automatic_writes_paused.is_(False),
        FastDumpingState.status_reason.in_((REASON, PREORDER_PAUSE)),
        Product.sale_state_overridden.is_(False),
    )
    if product_ids is not None:
        query = query.where(Product.id.in_(product_ids))
    rows = db.execute(query.order_by(Product.id).limit(100)).all()
    ids = {p.id for _, p, _ in rows}
    if not ids:
        return 0
    owners = inventory_owner_ids_for_products(db, ids)
    stock = physical_stock_counts(db, product_ids=ids, owner_by_product=owners)
    restored = 0
    for policy, product, state in rows:
        if stock.get(product.id, 0) <= 0:
            continue
        # Follow the inventory editor's product lock and recheck current FIFO.
        db.scalar(select(Product.id).where(Product.id == product.id,
            Product.workspace_id == workspace).with_for_update())
        db.refresh(policy)
        db.refresh(state)
        db.refresh(product)
        fresh_owners = inventory_owner_ids_for_products(db, {product.id})
        if (policy.enabled or state.automatic_writes_paused or product.sale_state_overridden
                or state.status_reason not in (REASON, PREORDER_PAUSE)
                or physical_stock_counts(db, product_ids={product.id},
                    owner_by_product=fresh_owners).get(product.id, 0) <= 0):
            continue
        if state.status_reason == PREORDER_PAUSE:
            from .preorder_modes import preorder_item, stock_arrived
            from .product_test_models import ProductTestJob
            item = preorder_item(db, product)
            if item is None or db.scalar(select(ProductTestJob.id).where(
                    ProductTestJob.workspace_id == workspace, ProductTestJob.item_id == item.id,
                    ProductTestJob.status.in_(("queued", "leased"))).limit(1)):
                continue
            stock_arrived(db, product.id)
        policy.enabled = True
        product.sale_enabled = True
        state.next_scan_at = utcnow()
        active = db.get(FastDumpingJob, state.active_job_id) if state.active_job_id else None
        if active is not None and active.status in {
                "queued_scan", "leased_scan", "leased_apply", "queued_verify", "leased_verify"}:
            active.reason = "inventory_priority:stock_returned"
            if active.status == "queued_scan":
                active.not_before_at = None
        queue_scan(db, policy=policy, workspace_id=workspace,
            reason="inventory_priority:stock_returned")
        restored += 1
    return restored


def supplier_owners(db, workspace, owners):
    if not owners:
        return set()
    # Bindings are shared by the inventory owner and its product aliases.
    return {
        int(owner or product_id) for product_id, owner in db.execute(
            select(Product.id, Product.inventory_owner_product_id)
            .join(ProductBinding, ProductBinding.product_id == Product.id)
            .where(Product.workspace_id == workspace,
                   ProductBinding.workspace_id == workspace,
                   ProductBinding.status.in_(BOUND_STATUSES),
                   or_(Product.id.in_(owners), Product.inventory_owner_product_id.in_(owners)))
        ).all()
    }


def disable_empty_products(db, workspace, product_ids=None):
    from .fast_dumping_service import cancel_active_job
    query = select(FastDumpingPolicy, Product).join(Product, Product.id == FastDumpingPolicy.product_id).where(
        FastDumpingPolicy.workspace_id == workspace, Product.workspace_id == workspace,
        FastDumpingPolicy.enabled.is_(True))
    if product_ids is not None:
        query = query.where(Product.id.in_(product_ids))
    rows = db.execute(query).all()
    ids = {product.id for _, product in rows}
    if not ids:
        return 0
    owners = inventory_owner_ids_for_products(db, ids)
    stock = physical_stock_counts(db, product_ids=ids, owner_by_product=owners)
    bound = supplier_owners(db, workspace, set(owners.values()))
    candidates = {product.id for _, product in rows
                  if stock.get(product.id, 0) <= 0 and owners.get(product.id, product.id) not in bound}
    if not candidates:
        return 0
    # Inventory and binding edits use the product lock too. Recheck in batches
    # after locking, without adding one SQL round trip for every empty card.
    locked_ids = set(db.scalars(select(Product.id).where(
        Product.id.in_(candidates), Product.workspace_id == workspace
    ).order_by(Product.id).with_for_update(skip_locked=True)).all())
    if not locked_ids:
        return 0
    fresh_stock = physical_stock_counts(db, product_ids=locked_ids, owner_by_product=owners)
    fresh_bound = supplier_owners(db, workspace, {owners.get(i, i) for i in locked_ids})
    eligible = {i for i in locked_ids if fresh_stock.get(i, 0) <= 0 and owners.get(i, i) not in fresh_bound}
    if not eligible:
        return 0
    policies = db.scalars(select(FastDumpingPolicy).where(
        FastDumpingPolicy.workspace_id == workspace,
        FastDumpingPolicy.product_id.in_(eligible), FastDumpingPolicy.enabled.is_(True)
    ).order_by(FastDumpingPolicy.id).with_for_update()).all()
    states = {state.product_id: state for state in db.scalars(select(FastDumpingState).where(
        FastDumpingState.workspace_id == workspace, FastDumpingState.product_id.in_(eligible)
    ).order_by(FastDumpingState.id).with_for_update()).all()}
    job_ids = {state.active_job_id for state in states.values() if state.active_job_id is not None}
    jobs = {job.id: job for job in db.scalars(select(FastDumpingJob).where(
        FastDumpingJob.workspace_id == workspace, FastDumpingJob.id.in_(job_ids))).all()}
    for policy in policies:
        policy.enabled = False
        state = states.get(policy.product_id)
        if state is not None:
            active = jobs.get(state.active_job_id)
            # Sent mutations keep their verification lease; unstarted work and
            # read-only scans can be cancelled immediately.
            if active is None or active.status not in {"leased_apply", "queued_verify", "leased_verify"}:
                cancel_active_job(db, state=state, reason=REASON)
            state.status = "paused"
            state.status_reason = REASON
            state.decision_status = "disabled_no_supply"
            state.inventory_on_hand = 0
            state.desired_stock_count = 0
            state.next_scan_at = None
            state.last_error_code = None
            state.last_error_message = None
    count = len(policies)
    db.flush()
    return count
