from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import or_, select
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

        # Stage filtering cannot happen in SQL against MarketplaceOrder.status.
        # The authoritative stage is resolved only after Snapshot and procurement
        # facts are loaded by the Decision Engine.
        order_rows = self._session.execute(
            select(MarketplaceOrder, MarketplaceAccount)
            .join(MarketplaceAccount, MarketplaceAccount.id == MarketplaceOrder.marketplace_account_id)
            .where(*filters)
            .order_by(MarketplaceOrder.ordered_at.desc().nullslast(), MarketplaceOrder.id.desc())
        ).all()
        if not order_rows:
            return 0, ()

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

        snapshot_by_order: dict[tuple[str, str], dict[str, Any]] = {}
        kaspi_keys = {
            (account.external_account_id, order.external_code)
            for order, account in order_rows
            if account.provider == "kaspi" and account.external_account_id and order.external_code
        }
        if kaspi_keys:
            merchant_ids = {merchant_id for merchant_id, _order_code in kaspi_keys}
            order_codes = {order_code for _merchant_id, order_code in kaspi_keys}
            snapshot_rows = self._session.execute(
                select(
                    KaspiSellerOrderSnapshotRecord.merchant_id,
                    KaspiSellerOrderSnapshotRecord.order_code,
                    KaspiSellerOrderSnapshotRecord.stage,
                    KaspiSellerOrderSnapshotRecord.state,
                    KaspiSellerOrderSnapshotRecord.status,
                    KaspiSellerOrderSnapshotRecord.snapshot_payload,
                    KaspiSellerOrderSnapshotRecord.observed_at,
                    KaspiSellerOrderSnapshotRecord.id,
                )
                .where(
                    KaspiSellerOrderSnapshotRecord.merchant_id.in_(merchant_ids),
                    KaspiSellerOrderSnapshotRecord.order_code.in_(order_codes),
                )
                .order_by(
                    KaspiSellerOrderSnapshotRecord.observed_at.desc(),
                    KaspiSellerOrderSnapshotRecord.id.desc(),
                )
            ).all()
            for merchant_id, order_code, stage, state, snapshot_status, payload_text, observed_at, _snapshot_id in snapshot_rows:
                key = (merchant_id, order_code)
                if key not in kaspi_keys or key in snapshot_by_order:
                    continue
                try:
                    payload = json.loads(payload_text) if payload_text else {}
                except (TypeError, ValueError):
                    payload = {}
                delivery = payload.get("delivery") if isinstance(payload, dict) else {}
                if not isinstance(delivery, dict):
                    delivery = {}
                snapshot_by_order[key] = {
                    "stage": stage,
                    "state": state,
                    "status": snapshot_status,
                    "observed_at": observed_at,
                    "assembled": _optional_bool(delivery.get("assembled", delivery.get("kdAssembled"))),
                    "transmitted": _optional_bool(
                        delivery.get("transmitted_to_courier", delivery.get("kdTransmittedToCourier"))
                    ),
                    "arrived": _optional_bool(delivery.get("is_order_arrived", delivery.get("isOrderArrived"))),
                    "returned": _optional_bool(
                        delivery.get("returned_to_warehouse", delivery.get("isReturnedToWarehouse"))
                    ),
                }

        def snapshot_for(account: MarketplaceAccount, order: MarketplaceOrder) -> dict[str, Any]:
            if account.provider != "kaspi" or not account.external_account_id or not order.external_code:
                return {}
            return snapshot_by_order.get((account.external_account_id, order.external_code), {})

        resolved: list[CommerceOrder] = []
        for order, account in order_rows:
            snapshot = snapshot_for(account, order)
            resolved.append(
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
                    snapshot_stage=snapshot.get("stage"),
                    snapshot_state=snapshot.get("state"),
                    snapshot_status=snapshot.get("status"),
                    snapshot_observed_at=snapshot.get("observed_at"),
                    snapshot_assembled=snapshot.get("assembled"),
                    snapshot_transmitted_to_courier=snapshot.get("transmitted"),
                    snapshot_arrived_at_pickup=snapshot.get("arrived"),
                    snapshot_returned_to_warehouse=snapshot.get("returned"),
                )
            )

        if status:
            normalized_status = status.strip().lower()
            resolved = [order for order in resolved if order.stage.value == normalized_status]

        total = len(resolved)
        return total, tuple(resolved[offset : offset + limit])


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (1, "1", "true", "TRUE", "True"):
        return True
    if value in (0, "0", "false", "FALSE", "False"):
        return False
    return None
