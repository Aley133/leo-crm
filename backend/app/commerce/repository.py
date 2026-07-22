from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..kaspi_seller.snapshot_models import KaspiSellerOrderSnapshotRecord
from ..models import MarketplaceAccount, MarketplaceOrder, MarketplaceOrderLine
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
        lines_by_order: dict[int, list[CommerceOrderLine]] = defaultdict(list)
        line_rows = self._session.execute(
            select(MarketplaceOrderLine, PurchaseRequest.id, PurchaseRequest.status, PurchaseRequest.version)
            .outerjoin(PurchaseRequestLine, PurchaseRequestLine.marketplace_order_line_id == MarketplaceOrderLine.id)
            .outerjoin(PurchaseRequest, PurchaseRequest.id == PurchaseRequestLine.purchase_request_id)
            .where(MarketplaceOrderLine.marketplace_order_id.in_(order_ids))
            .order_by(MarketplaceOrderLine.id)
        ).all()
        for line, purchase_request_id, purchase_status, purchase_version in line_rows:
            lines_by_order[line.marketplace_order_id].append(
                CommerceOrderLine(
                    line_id=line.id,
                    product_id=line.product_id,
                    external_product_id=line.external_product_id,
                    merchant_sku=line.merchant_sku,
                    title=line.title,
                    quantity=line.quantity,
                    unit_price=Decimal(line.unit_price),
                    line_total=Decimal(line.line_total),
                    purchase_request_id=None if purchase_request_id is None else str(purchase_request_id),
                    purchase_status=purchase_status,
                    purchase_version=purchase_version,
                )
            )

        snapshot_keys = {
            (account.id, order.external_code)
            for order, account in order_rows
            if account.provider == "kaspi" and order.external_code
        }
        latest_snapshots: dict[tuple[int, str], KaspiSellerOrderSnapshotRecord] = {}
        if snapshot_keys:
            account_ids = {account_id for account_id, _order_code in snapshot_keys}
            order_codes = {order_code for _account_id, order_code in snapshot_keys}
            snapshot_rows = self._session.scalars(
                select(KaspiSellerOrderSnapshotRecord)
                .where(
                    KaspiSellerOrderSnapshotRecord.marketplace_account_id.in_(account_ids),
                    KaspiSellerOrderSnapshotRecord.order_code.in_(order_codes),
                )
                .order_by(
                    KaspiSellerOrderSnapshotRecord.observed_at.desc(),
                    KaspiSellerOrderSnapshotRecord.id.desc(),
                )
            ).all()
            for snapshot in snapshot_rows:
                if snapshot.marketplace_account_id is None:
                    continue
                key = (snapshot.marketplace_account_id, snapshot.order_code)
                if key in snapshot_keys and key not in latest_snapshots:
                    latest_snapshots[key] = snapshot

        return total, tuple(
            self._to_commerce_order(
                order=order,
                account=account,
                lines=tuple(lines_by_order.get(order.id, ())),
                snapshot=latest_snapshots.get((account.id, order.external_code))
                if order.external_code
                else None,
            )
            for order, account in order_rows
        )

    @staticmethod
    def _to_commerce_order(
        *,
        order: MarketplaceOrder,
        account: MarketplaceAccount,
        lines: tuple[CommerceOrderLine, ...],
        snapshot: KaspiSellerOrderSnapshotRecord | None,
    ) -> CommerceOrder:
        return CommerceOrder(
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
            lines=lines,
            observed_stage=snapshot.stage if snapshot is not None else None,
            observed_at=snapshot.observed_at if snapshot is not None else None,
        )
