from __future__ import annotations

from decimal import Decimal

from .domain import CommerceOrder, CommerceSummary
from .repository import CommerceRepository


ACTIVE_STATUSES = {"new", "accepted", "assembly", "shipping"}


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
            active_orders=sum(1 for order in orders if order.status in ACTIVE_STATUSES),
            delivered_orders=sum(1 for order in orders if order.status == "delivered"),
            cancelled_orders=sum(
                1 for order in orders if order.status in {"cancelled", "returned"}
            ),
            unresolved_lines=sum(order.unresolved_lines for order in orders),
            procurement_required_lines=sum(
                order.procurement_required_lines for order in orders
            ),
        )
