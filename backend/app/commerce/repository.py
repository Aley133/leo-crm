from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

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


class SqlAlchemyCommerceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

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

        order_ids = [order.id for order, _account in order_rows]
        external_order_ids = [order.external_order_id for order, _account in order_rows]
        raw_payload_by_external_id: dict[str, dict] = {}
        raw_rows = self._session.execute(
            select(
                MarketplaceRawPayload.external_object_id,
                MarketplaceRawPayload.payload_json,
                MarketplaceRawPayload.received_at,
                MarketplaceRawPayload.id,
            )
            .where(
                MarketplaceRawPayload.payload_type == "order",
                MarketplaceRawPayload.external_object_id.in_(external_order_ids),
            )
            .order_by(
                MarketplaceRawPayload.external_object_id,
                MarketplaceRawPayload.received_at.desc(),
                MarketplaceRawPayload.id.desc(),
            )
        ).all()
        for external_object_id, payload_json, _received_at, _raw_id in raw_rows:
            raw_payload_by_external_id.setdefault(external_object_id, payload_json)

        order_external_by_id = {
            order.id: order.external_order_id for order, _account in order_rows
        }
        line_rows = self._session.execute(
            select(MarketplaceOrderLine, PurchaseRequest.id, PurchaseRequest.status, PurchaseRequest.version)
            .outerjoin(PurchaseRequestLine, PurchaseRequestLine.marketplace_order_line_id == MarketplaceOrderLine.id)
            .outerjoin(PurchaseRequest, PurchaseRequest.id == PurchaseRequestLine.purchase_request_id)
            .where(MarketplaceOrderLine.marketplace_order_id.in_(order_ids))
            .order_by(MarketplaceOrderLine.id)
        ).all()

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
                payload = raw_payload_by_external_id.get(
                    order_external_by_id[line.marketplace_order_id]
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
            procurement_unit_cost = None
            procurement_source_name = None
            if effective_product_id is not None:
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
