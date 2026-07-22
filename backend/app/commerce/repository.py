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
)
from ..purchase_models import PurchaseRequest, PurchaseRequestLine
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
        lines_by_order: dict[int, list[CommerceOrderLine]] = defaultdict(list)
        line_rows = self._session.execute(
            select(MarketplaceOrderLine, PurchaseRequest.id, PurchaseRequest.status, PurchaseRequest.version)
            .outerjoin(PurchaseRequestLine, PurchaseRequestLine.marketplace_order_line_id == MarketplaceOrderLine.id)
            .outerjoin(PurchaseRequest, PurchaseRequest.id == PurchaseRequestLine.purchase_request_id)
            .where(MarketplaceOrderLine.marketplace_order_id.in_(order_ids))
            .order_by(MarketplaceOrderLine.id)
        ).all()
        for line, purchase_request_id, purchase_status, purchase_version in line_rows:
            title = line.title
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

            lines_by_order[line.marketplace_order_id].append(
                CommerceOrderLine(
                    line_id=line.id,
                    product_id=line.product_id,
                    external_product_id=line.external_product_id,
                    merchant_sku=line.merchant_sku,
                    title=title,
                    quantity=line.quantity,
                    unit_price=Decimal(line.unit_price),
                    line_total=Decimal(line.line_total),
                    purchase_request_id=None if purchase_request_id is None else str(purchase_request_id),
                    purchase_status=purchase_status,
                    purchase_version=purchase_version,
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
