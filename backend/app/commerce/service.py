from __future__ import annotations

from decimal import Decimal

from .domain import CommerceOrder, CommerceSummary
from .repository import CommerceRepository


ACTIVE_STAGES = {"new", "accepted", "preorder", "assembly", "handover", "shipping"}
DIRECT_RAW_STATUS_STAGES = {"shipping", "delivered", "cancelled", "returned"}


class CommerceService:
    def __init__(self, repository: CommerceRepository) -> None:
        self._repository = repository

    def list_orders(
        self,
        *,
        limit: int,
        offset: int,
        status: str | None = None,
        query: str | None = None,
    ) -> tuple[int, tuple[CommerceOrder, ...], CommerceSummary]:
        if status and status not in DIRECT_RAW_STATUS_STAGES:
            # Operational stages such as accepted, preorder and assembly depend
            # on Commerce facts and therefore belong to the domain rather than
            # the marketplace SQL model.
            _raw_total, candidates = self._repository.list_orders(
                limit=1000,
                offset=0,
                status=None,
                query=query,
            )
            filtered = tuple(order for order in candidates if order.stage.value == status)
            orders = filtered[offset : offset + limit]
            total = len(filtered)
        else:
            total, orders = self._repository.list_orders(
                limit=limit,
                offset=offset,
                status=status,
                query=query,
            )
        return total, orders, self.summarize(orders)

    @staticmethod
    def summarize(orders: tuple[CommerceOrder, ...]) -> CommerceSummary:
        return CommerceSummary(
            orders_count=len(orders),
            units_count=sum(order.units for order in orders),
            revenue=sum((order.total_amount for order in orders), Decimal("0")),
            active_orders=sum(1 for order in orders if order.stage.value in ACTIVE_STAGES),
            delivered_orders=sum(1 for order in orders if order.stage.value == "delivered"),
            cancelled_orders=sum(
                1 for order in orders if order.stage.value in {"cancelled", "returned"}
            ),
            unresolved_lines=sum(order.unresolved_lines for order in orders),
            procurement_required_lines=sum(
                order.procurement_required_lines for order in orders
            ),
        )
