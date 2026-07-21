from __future__ import annotations

from typing import Any

from .models import SellerOrderFacts, SellerOrderStep


def map_seller_order_facts(payload: dict[str, Any]) -> SellerOrderFacts:
    """Map either a raw Seller GraphQL orderDetail or importer attributes into facts."""
    order_detail = _unwrap_order_detail(payload)
    delivery = order_detail.get("delivery")
    delivery = delivery if isinstance(delivery, dict) else {}

    raw_steps = order_detail.get("orderSteps")
    steps = tuple(
        SellerOrderStep.from_payload(step)
        for step in raw_steps
        if isinstance(step, dict)
    ) if isinstance(raw_steps, list) else ()

    raw_markers = order_detail.get("markers")
    marker_names = tuple(
        str(marker.get("marker")).strip().upper()
        for marker in raw_markers
        if isinstance(marker, dict) and marker.get("marker") not in (None, "")
    ) if isinstance(raw_markers, list) else ()

    return SellerOrderFacts(
        order_code=_optional_text(order_detail.get("code") or order_detail.get("orderCode")),
        state=str(order_detail.get("state") or "UNKNOWN").strip().upper(),
        status=str(order_detail.get("status") or order_detail.get("orderStatus") or "UNKNOWN").strip().upper(),
        preorder=order_detail.get("preOrder") is True,
        is_order_arrived=delivery.get("isOrderArrived") is True,
        kd_assembled=delivery.get("kdAssembled") is True,
        kd_transmitted_to_courier=delivery.get("kdTransmittedToCourier") is True,
        steps=steps,
        marker_names=marker_names,
    )


def _unwrap_order_detail(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict):
        merchant = data.get("merchant")
        if isinstance(merchant, dict):
            detail = merchant.get("orderDetail")
            if isinstance(detail, dict):
                return detail

    attributes = payload.get("attributes")
    if isinstance(attributes, dict):
        return attributes
    return payload


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)
