from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Protocol

from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.orm import Session

from ..inventory_models import InventoryAllocation, InventoryBatch, InventoryBatchType
from ..inventory_service import build_incoming_reservations
from ..kaspi_order_line_display import recover_order_line_title
from ..models import (
    MarketplaceAccount,
    MarketplaceOrder,
    MarketplaceOrderLine,
    MarketplaceRawPayload,
    Product,
)
from ..monitoring import SupplierOfferState
from ..purchase_models import PurchaseRequest, PurchaseRequestLine
from ..suppliers import ProductBinding, Supplier, SupplierProduct
from .domain import CommerceOrder, CommerceOrderLine


class CommerceRepository(Protocol):
    def list_orders(
        self,
        *,
        limit: int,
        offset: int,
        status: str | None = None,
        query: str | None = None,
    ) -> tuple[int, tuple[CommerceOrder, ...]]: ...


def _latest_order_raw_payloads(
    session: Session,
    *,
    order_keys: set[tuple[int, str]],
) -> dict[tuple[int, str], dict]:
    """Load one newest JSON snapshot for each visible marketplace order.

    Order synchronization keeps immutable source snapshots for audit purposes.
    Fetching that complete history for the 200-row Orders screen made response
    memory grow with every synchronization cycle. Rank the lightweight row IDs
    in SQL first and deserialize only the newest JSON document per order.
    """
    if not order_keys:
        return {}

    ranked = (
        select(
            MarketplaceRawPayload.id.label("raw_payload_id"),
            MarketplaceRawPayload.marketplace_account_id.label(
                "marketplace_account_id"
            ),
            MarketplaceRawPayload.external_object_id.label("external_object_id"),
            func.row_number()
            .over(
                partition_by=(
                    MarketplaceRawPayload.marketplace_account_id,
                    MarketplaceRawPayload.external_object_id,
                ),
                order_by=(
                    MarketplaceRawPayload.received_at.desc(),
                    MarketplaceRawPayload.id.desc(),
                ),
            )
            .label("raw_payload_rank"),
        )
        .where(
            MarketplaceRawPayload.payload_type == "order",
            tuple_(
                MarketplaceRawPayload.marketplace_account_id,
                MarketplaceRawPayload.external_object_id,
            ).in_(sorted(order_keys)),
        )
        .subquery("ranked_order_raw_payloads")
    )
    rows = session.execute(
        select(
            ranked.c.marketplace_account_id,
            ranked.c.external_object_id,
            MarketplaceRawPayload.payload_json,
        )
        .join(
            MarketplaceRawPayload,
            MarketplaceRawPayload.id == ranked.c.raw_payload_id,
        )
        .where(ranked.c.raw_payload_rank == 1)
    ).all()
    return {
        (int(account_id), str(external_object_id)): payload_json
        for account_id, external_object_id, payload_json in rows
        if isinstance(payload_json, dict)
    }


class SqlAlchemyCommerceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def _incoming_reservations(self) -> dict[int, int]:
        """Virtually reserve expected stock to preorder lines in order-date FIFO order."""
        reserved: dict[int, int] = defaultdict(int)
        for reservation in build_incoming_reservations(self._session):
            reserved[reservation.order_line_id] += reservation.reserved_quantity
        return reserved

    def list_orders(
        self,
        *,
        limit: int,
        offset: int,
        status: str | None = None,
        query: str | None = None,
    ) -> tuple[int, tuple[CommerceOrder, ...]]:
        filters = []
        if status:
            filters.append(MarketplaceOrder.status == status)
        if query:
            pattern = f"%{query.strip()}%"
            matching_order_ids = select(MarketplaceOrderLine.marketplace_order_id).where(
                or_(
                    MarketplaceOrderLine.title.ilike(pattern),
                    MarketplaceOrderLine.merchant_sku.ilike(pattern),
                    MarketplaceOrderLine.external_product_id.ilike(pattern),
                )
            )
            filters.append(
                or_(
                    MarketplaceOrder.external_code.ilike(pattern),
                    MarketplaceOrder.external_order_id.ilike(pattern),
                    MarketplaceOrder.id.in_(matching_order_ids),
                )
            )

        total = self._session.scalar(select(func.count(MarketplaceOrder.id)).where(*filters)) or 0
        order_rows = self._session.execute(
            select(MarketplaceOrder, MarketplaceAccount)
            .join(MarketplaceAccount, MarketplaceAccount.id == MarketplaceOrder.marketplace_account_id)
            .where(*filters)
            .order_by(MarketplaceOrder.ordered_at.desc().nullslast(), MarketplaceOrder.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        if not order_rows:
            return total, ()

        incoming_reserved_by_line = self._incoming_reservations()
        order_ids = [order.id for order, _account in order_rows]
        order_key_by_id = {
            order.id: (account.id, order.external_order_id)
            for order, account in order_rows
        }
        raw_payload_by_order_key = _latest_order_raw_payloads(
            self._session,
            order_keys=set(order_key_by_id.values()),
        )
        line_rows = self._session.execute(
            select(MarketplaceOrderLine, PurchaseRequest.id, PurchaseRequest.status, PurchaseRequest.version)
            .outerjoin(PurchaseRequestLine, PurchaseRequestLine.marketplace_order_line_id == MarketplaceOrderLine.id)
            .outerjoin(PurchaseRequest, PurchaseRequest.id == PurchaseRequestLine.purchase_request_id)
            .where(MarketplaceOrderLine.marketplace_order_id.in_(order_ids))
            .order_by(MarketplaceOrderLine.id)
        ).all()
        line_ids = [line.id for line, *_purchase in line_rows]

        inventory_by_line: dict[int, tuple[int, Decimal, str | None, int]] = {}
        if line_ids:
            allocation_rows = self._session.execute(
                select(
                    InventoryAllocation.marketplace_order_line_id,
                    InventoryBatch.batch_type,
                    InventoryBatch.source_name,
                    func.sum(InventoryAllocation.quantity),
                    func.sum(InventoryAllocation.quantity * InventoryAllocation.unit_cost),
                )
                .join(
                    InventoryBatch,
                    InventoryBatch.id == InventoryAllocation.inventory_batch_id,
                )
                .where(InventoryAllocation.marketplace_order_line_id.in_(line_ids))
                .group_by(
                    InventoryAllocation.marketplace_order_line_id,
                    InventoryBatch.batch_type,
                    InventoryBatch.source_name,
                )
            ).all()
            allocation_totals: dict[int, dict[str, object]] = {}
            for line_id, batch_type, source_name, quantity, total_cost in allocation_rows:
                values = allocation_totals.setdefault(
                    int(line_id),
                    {
                        "quantity": 0,
                        "cost": Decimal("0"),
                        "sources": [],
                        "production": 0,
                    },
                )
                values["quantity"] = int(values["quantity"]) + int(quantity or 0)
                values["cost"] = Decimal(values["cost"]) + Decimal(total_cost or 0)
                if batch_type == InventoryBatchType.PRODUCTION.value:
                    values["production"] = int(values["production"]) + int(quantity or 0)
                source = (
                    (source_name or "").strip() or "Производство"
                    if batch_type == InventoryBatchType.PRODUCTION.value
                    else "Склад FIFO"
                )
                sources = values["sources"]
                if isinstance(sources, list) and source not in sources:
                    sources.append(source)
            inventory_by_line = {
                line_id: (
                    int(values["quantity"]),
                    Decimal(values["cost"]),
                    " + ".join(values["sources"]) if values["sources"] else None,
                    int(values["production"]),
                )
                for line_id, values in allocation_totals.items()
            }

        identities: set[str] = set()
        explicit_product_ids: set[int] = set()
        for line, *_purchase in line_rows:
            if line.product_id is not None:
                explicit_product_ids.add(line.product_id)
            if line.merchant_sku:
                identities.add(line.merchant_sku.strip())
            if line.external_product_id:
                identities.add(line.external_product_id.strip())

        product_rows = self._session.scalars(
            select(Product).where(
                or_(
                    Product.id.in_(explicit_product_ids) if explicit_product_ids else False,
                    Product.merchant_sku.in_(identities) if identities else False,
                    Product.kaspi_product_id.in_(identities) if identities else False,
                )
            )
        ).all()
        product_by_id = {product.id: product for product in product_rows}
        product_by_identity: dict[str, Product] = {}
        for product in product_rows:
            if product.merchant_sku:
                product_by_identity.setdefault(product.merchant_sku.strip(), product)
            if product.kaspi_product_id:
                product_by_identity.setdefault(product.kaspi_product_id.strip(), product)

        product_ids = set(product_by_id)
        source_by_product: dict[int, tuple[Decimal | None, str | None]] = {}
        if product_ids:
            source_rows = self._session.execute(
                select(ProductBinding, SupplierProduct, Supplier, SupplierOfferState)
                .join(SupplierProduct, SupplierProduct.id == ProductBinding.supplier_product_id)
                .join(Supplier, Supplier.id == SupplierProduct.supplier_id)
                .outerjoin(
                    SupplierOfferState,
                    SupplierOfferState.supplier_product_id == SupplierProduct.id,
                )
                .where(
                    ProductBinding.product_id.in_(product_ids),
                    ProductBinding.status.in_(("active", "confirmed")),
                )
                .order_by(
                    ProductBinding.product_id,
                    ProductBinding.is_primary.desc(),
                    ProductBinding.priority,
                    ProductBinding.id,
                )
            ).all()
            for binding, supplier_product, supplier, state in source_rows:
                if binding.product_id in source_by_product:
                    continue
                price = None
                if state is not None and state.price is not None and state.available is not False:
                    price = Decimal(state.price)
                elif supplier_product.current_price is not None and supplier_product.in_stock is not False:
                    price = Decimal(supplier_product.current_price)
                if price is not None:
                    source_by_product[binding.product_id] = (price, supplier.name)

        lines_by_order: dict[int, list[CommerceOrderLine]] = defaultdict(list)
        for line, purchase_request_id, purchase_status, purchase_version in line_rows:
            product = product_by_id.get(line.product_id) if line.product_id is not None else None
            if product is None:
                for identity in (line.merchant_sku, line.external_product_id):
                    if identity and identity.strip() in product_by_identity:
                        product = product_by_identity[identity.strip()]
                        break

            title = product.name if product is not None else line.title
            if not title or title.strip().lower() == "unknown product":
                payload = raw_payload_by_order_key.get(
                    order_key_by_id[line.marketplace_order_id]
                )
                recovered = recover_order_line_title(
                    payload,
                    identities=(
                        line.external_line_id,
                        line.external_product_id,
                        line.merchant_sku,
                    ),
                )
                if recovered:
                    title = recovered

            effective_product_id = product.id if product is not None else line.product_id
            (
                inventory_quantity,
                inventory_total_cost,
                inventory_source_name,
                production_completed_quantity,
            ) = (
                inventory_by_line.get(line.id, (0, Decimal("0"), None, 0))
            )
            procurement_unit_cost = None
            procurement_source_name = None
            if line.quantity > 0 and inventory_quantity >= line.quantity:
                procurement_unit_cost = (inventory_total_cost / Decimal(line.quantity)).quantize(Decimal("0.01"))
                procurement_source_name = inventory_source_name or "Склад FIFO"
            elif effective_product_id is not None:
                procurement_unit_cost, procurement_source_name = source_by_product.get(
                    effective_product_id, (None, None)
                )

            lines_by_order[line.marketplace_order_id].append(
                CommerceOrderLine(
                    line_id=line.id,
                    product_id=effective_product_id,
                    external_product_id=line.external_product_id,
                    merchant_sku=line.merchant_sku,
                    title=title,
                    quantity=line.quantity,
                    unit_price=Decimal(line.unit_price),
                    line_total=Decimal(line.line_total),
                    purchase_request_id=None if purchase_request_id is None else str(purchase_request_id),
                    purchase_status=purchase_status,
                    purchase_version=purchase_version,
                    procurement_unit_cost=procurement_unit_cost,
                    procurement_source_name=procurement_source_name,
                    inventory_allocated_quantity=inventory_quantity,
                    production_completed_quantity=production_completed_quantity,
                    incoming_reserved_quantity=incoming_reserved_by_line.get(line.id, 0),
                )
            )

        result = tuple(
            CommerceOrder(
                order_id=order.id,
                external_code=order.external_code,
                marketplace=account.provider,
                marketplace_account_id=account.id,
                marketplace_external_account_id=account.external_account_id,
                status=order.status,
                original_status=order.original_status,
                currency=order.currency,
                total_amount=Decimal(order.total_amount),
                ordered_at=order.ordered_at,
                delivered_at=order.delivered_at,
                lines=tuple(lines_by_order.get(order.id, ())),
            )
            for order, account in order_rows
        )
        return total, result
