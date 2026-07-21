from __future__ import annotations

from .models import SellerOrderFacts


TERMINAL_CANCELLED = {"CANCELLED", "CANCELED"}
TERMINAL_RETURNED = {"CANCELLING", "KASPI_DELIVERY_RETURN_REQUESTED", "RETURNED"}
TERMINAL_DELIVERED = {"DELIVERED", "COMPLETED", "ARCHIVE", "ARCHIVED"}


def resolve_seller_stage(facts: SellerOrderFacts) -> str | None:
    """Return the importer token derived only from verified Kaspi Seller facts."""
    if facts.status in TERMINAL_CANCELLED:
        return "CANCELLED"
    if facts.status in TERMINAL_RETURNED:
        return "RETURNED"
    if facts.status in TERMINAL_DELIVERED:
        return "DELIVERED"

    transmission_actual = facts.step_actual_time("TRANSMISSION")
    preorder_actual = facts.step_actual_time("PRE_ORDER")

    if (
        facts.state == "KASPI_DELIVERY_TRANSMITTED"
        or facts.kd_transmitted_to_courier
        or transmission_actual is not None
    ):
        return "SHIPPING"

    if (
        facts.state == "KASPI_DELIVERY_WAIT_FOR_COURIER"
        or facts.kd_assembled
        or "CARGO_ASSEMBLED" in facts.marker_names
        or facts.status == "ASSEMBLED"
    ):
        return "HANDOVER"

    if (
        facts.state == "KASPI_DELIVERY_CARGO_ASSEMBLY"
        or facts.is_order_arrived
        or preorder_actual is not None
    ):
        return "ASSEMBLY"

    if (
        facts.state == "KASPI_DELIVERY_WAIT_FOR_POINT_DELIVERY"
        and facts.preorder
        and preorder_actual is None
    ):
        return "ACCEPTED_BY_MERCHANT"

    return None
