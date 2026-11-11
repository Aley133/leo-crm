"""Read-only inventory of preorders across manual, Test, Fast and legacy flows."""
from datetime import datetime, UTC
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree
from types import SimpleNamespace

from sqlalchemy import select, func
from sqlalchemy.orm import load_only

from .dumping_models import DumpingPolicy, DumpingRun, KaspiXmlFeed
from .fast_dumping_models import FastDumpingJob, FastDumpingState, FastDumpingPolicy
from .models import Product
from .product_test_models import ProductTestItem


def _number(value):
    try:
        return max(0, int(Decimal(str(value))))
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return None


def _time(value):
    if value is None:
        return 0
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value).timestamp()


def _xml_offers(feed):
    if feed is None:
        return {}, []
    try:
        root = ElementTree.fromstring(feed.generated_xml or feed.source_xml)
    except ElementTree.ParseError:
        return {}, ["Сохранённый каталог не удалось прочитать; показаны данные HTTP и CRM."]
    offers = {}
    local = lambda tag: tag.rsplit("}", 1)[-1].lower()
    for element in root.iter():
        if local(element.tag) != "offer":
            continue
        sku = str(element.get("sku") or "").strip()
        if not sku:
            continue
        values = {local(child.tag): (child.text or "").strip() for child in element.iter()}
        # Do not mix the stock of one warehouse with the preorder of another.
        availability = next((child for child in element.iter() if local(child.tag) == "availability"
            and (_number(child.get("preOrder")) or 0) > 0
            and str(child.get("available", "yes")).lower() not in {"no", "false", "0"}), None)
        if availability is None:
            continue
        offers[sku] = {"name": values.get("model") or sku, "merchant_sku": sku,
            "preorder_days": _number(availability.get("preOrder")),
            "stock_count": _number(availability.get("stockCount")),
            "test_price_kzt": _number(values.get("price") or values.get("cityprice")),
            "source": "catalog", "source_label": "Сохранённый каталог",
            "observed_at": feed.imported_at, "confirmed": False}
    return offers, []


def _latest_rows(db, model, workspace, condition, fields):
    latest = select(func.max(model.id).label("id")).where(
        model.workspace_id == workspace, condition).group_by(model.product_id).subquery()
    return db.scalars(select(model).options(load_only(*fields, raiseload=True)).join(latest, latest.c.id == model.id)
        .where(model.workspace_id == workspace)).all()


def read_inventory(db, workspace):
    from .preorder_api import _preorder_visibility, _preorder_rows
    from .dumping_service import physical_stock_counts
    products = db.scalars(select(Product).where(Product.workspace_id == workspace).order_by(Product.id)).all()
    by_id = {p.id: p for p in products}
    product_skus = {p.merchant_sku for p in products if p.merchant_sku}
    stock = physical_stock_counts(db, product_ids=set(by_id))
    # Project pricing controls in SQL. Test history may contain megabytes of
    # market snapshots in offers_json; none belong in the inventory table.
    flags = ("stock_mode", "dump_enabled", "rocket_enabled", "management_disabled")
    texts = ("ozon_url", "minimum_profit_kzt")
    columns = [c for c in ProductTestItem.__table__.columns if c.name != "offers_json"]
    statement = select(*columns,
        *(ProductTestItem.offers_json[key].as_boolean().label("pricing_"+key) for key in flags),
        *(ProductTestItem.offers_json[key].as_string().label("pricing_"+key) for key in texts)
    ).where(ProductTestItem.workspace_id == workspace, _preorder_visibility()).order_by(ProductTestItem.id.desc())
    items = []
    for record in db.execute(statement).mappings():
        values = dict(record)
        values["offers_json"] = {key: values.pop("pricing_"+key) for key in (*flags, *texts)}
        items.append(SimpleNamespace(**values))
    manual_rows = _preorder_rows(db, items, workspace, products=by_id, counts=stock)
    feed = db.scalar(select(KaspiXmlFeed).where(KaspiXmlFeed.workspace_id == workspace,
        KaspiXmlFeed.active.is_(True)).order_by(KaspiXmlFeed.id.desc()).limit(1))
    catalog, warnings = _xml_offers(feed)
    fast = {j.product_id: j for j in _latest_rows(db, FastDumpingJob, workspace,
        FastDumpingJob.status == "applied", (FastDumpingJob.product_id, FastDumpingJob.decision_json, FastDumpingJob.completed_at))}
    classic = {r.product_id: r for r in _latest_rows(db, DumpingRun, workspace,
        DumpingRun.published.is_(True), (DumpingRun.product_id, DumpingRun.preorder_days,
            DumpingRun.target_price_kzt, DumpingRun.own_price_kzt, DumpingRun.created_at))}
    states = {s.product_id: s for s in db.scalars(select(FastDumpingState).where(
        FastDumpingState.workspace_id == workspace)).all()}
    policies = {p.product_id: p for p in db.scalars(select(FastDumpingPolicy).where(
        FastDumpingPolicy.workspace_id == workspace)).all()}
    classic_policies = {p.product_id: p for p in db.scalars(select(DumpingPolicy).where(
        DumpingPolicy.workspace_id == workspace)).all()}
    rows, seen_products, seen_skus = [], set(), set()

    def finish(row, product=None):
        product_id = product.id if product else None
        state, policy = states.get(product_id), policies.get(product_id)
        physical = stock.get(product_id, 0)
        row.update(row_key=f"product:{product_id}" if product else f"item:{row['id']}" if row.get('id') else f"sku:{row['merchant_sku']}",
            product_id=product_id, physical_stock=physical,
            sale_enabled=product.sale_enabled if product else None,
            automation_enabled=bool((policy and policy.enabled) or (classic_policies.get(product_id) and classic_policies[product_id].enabled)),
            automation_mode="rocket" if policy and policy.pricing_mode == "automation" else "dumping" if policy and policy.enabled else "fixed")
        row["name"] = product.name if product else row.get("name") or row["merchant_sku"]
        row["kaspi_product_id"] = product.kaspi_product_id if product else row.get("kaspi_product_id")
        row["image_url"] = product.image_url if product else row.get("image_url")
        row["merchant_sku"] = product.merchant_sku if product else row.get("merchant_sku")
        if physical > 0:
            row["group"] = "stock"
        elif product and not product.sale_enabled:
            row["group"] = "disabled"
        elif row.get("pending"):
            row["group"] = "working"
        elif row.get("last_error") or (state and state.automatic_writes_paused):
            row["group"] = "attention"
            row["last_error"] = row.get("last_error") or state.pause_reason
        elif row.get("status") in {"preorder_disabled", "sale_removed", "ready_to_resume", "stock_trading"}:
            row["group"] = "disabled"
        else:
            row["group"] = "active"
        row["can_connect"] = physical <= 0 and not row.get("pending", False)
        if (state and state.own_price_kzt is not None and row["group"] == "active"
                and _time(state.last_scanned_at) >= _time(row.get("observed_at"))):
            row["test_price_kzt"] = state.own_price_kzt
        rows.append(row)
        if product:
            seen_products.add(product.id)
        if row.get("merchant_sku"):
            seen_skus.add(row["merchant_sku"])

    for row in manual_rows:
        product = by_id.get(row.get("product_id"))
        if product and product.id in seen_products:
            continue
        row.update(source="manual" if row["input_reference"].startswith("manual-preorder:") else "test",
            source_label="Предзаказ" if row["input_reference"].startswith("manual-preorder:") else "Тест товара",
            observed_at=row.get("added_at"), confirmed=bool(row.get("added_at")))
        job = fast.get(product.id) if product else None
        if job and _time(job.completed_at) > _time(row.get("observed_at")):
            decision = job.decision_json or {}
            if (decision.get("fulfillment_mode", "preorder") != "preorder"
                    or not (_number(decision.get("preorder_days")) or 0)
                    or _number(decision.get("stock_count")) == 0):
                if not row.get("pending") and row["status"] not in {"error", "preorder_disabled", "sale_removed"}:
                    row["status"] = "ready_to_resume"
                row["confirmed"] = False
        finish(row, product)

    for product in products:
        if product.id in seen_products:
            continue
        # Explicitly shared historical aliases of the same merchant offer are
        # represented by their canonical inventory record, never by master ID alone.
        if (product.inventory_owner_product_id in seen_products and product.merchant_sku in seen_skus
                and product.kaspi_product_id == product.merchant_sku):
            continue
        snapshot = catalog.get(product.merchant_sku)
        job = fast.get(product.id)
        run = classic.get(product.id)
        if job is not None and (run is None or _time(job.completed_at) >= _time(run.created_at)):
            decision = job.decision_json or {}
            # A more recent HTTP inventory/zero write invalidates old preorder XML.
            snapshot = ({"preorder_days": _number(decision.get("preorder_days")),
                "stock_count": _number(decision.get("stock_count")),
                "test_price_kzt": _number(decision.get("target_price_kzt")),
                "source": "fast", "source_label": "Fast Dumping · HTTP",
                "observed_at": job.completed_at, "confirmed": True}
                if decision.get("fulfillment_mode", "preorder") == "preorder" and (_number(decision.get("preorder_days")) or 0) > 0
                    and _number(decision.get("stock_count")) != 0 else None)
        elif run is not None:
            snapshot = ({"preorder_days": run.preorder_days, "stock_count": None,
                "test_price_kzt": run.target_price_kzt or run.own_price_kzt,
                "source": "classic", "source_label": "Обычный демпинг",
                "observed_at": run.created_at, "confirmed": False}
                if (run.preorder_days or 0) > 0 else None)
        state = states.get(product.id)
        if snapshot is None:
            # A supplier binding or a delivery estimate alone is not proof of
            # a live preorder. Keep it in the separate pending group.
            if job is not None or state is None or state.source_kind != "supplier" or not state.desired_stock_count:
                continue
            snapshot = {"preorder_days": None, "stock_count": state.desired_stock_count,
                "test_price_kzt": state.own_price_kzt, "source": "fast",
                "source_label": "Fast Dumping · ожидает подтверждения", "confirmed": False,
                "observed_at": state.last_scanned_at, "pending": True}
        finish({"id": None, "status": "preorder_connected", "offers": {},
            "last_error": None, **snapshot}, product)

    # Previously imported offers without a CRM product must remain visible too.
    for sku, snapshot in catalog.items():
        if sku not in seen_skus:
            if sku in product_skus:  # Suppressed by a newer confirmed inventory/zero state.
                continue
            finish({"id": None, "status": "preorder_connected", "offers": {},
                "last_error": None, **snapshot})
    groups = ("active", "working", "attention", "stock", "disabled")
    rows.sort(key=lambda r: (groups.index(r["group"]), r["name"].casefold(), r["row_key"]))
    return {"items": rows, "summary": {"total": len(rows), **{g: sum(r["group"] == g for r in rows) for g in groups}},
        "warnings": warnings, "checked_at": datetime.now(UTC)}
