from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CommerceOrderStage(StrEnum):
    NEW = "new"
    ACCEPTED = "accepted"
    PREORDER = "preorder"
    RECEIVED = "received"
    ASSEMBLY = "assembly"
    HANDOVER = "handover"
    SHIPPING = "shipping"
    PICKUP = "pickup"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    RETURNED = "returned"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class OrderDecisionFacts:
    marketplace_status: str | None
    snapshot_stage: str | None = None
    snapshot_state: str | None = None
    snapshot_status: str | None = None
    assembled: bool | None = None
    transmitted_to_courier: bool | None = None
    arrived_at_pickup: bool | None = None
    returned_to_warehouse: bool | None = None
    has_lines: bool = False
    all_procurement_received: bool = False
    has_procurement_in_progress: bool = False


class OrderDecisionEngine:
    """Resolve one authoritative CRM business stage from explicit facts.

    Kaspi state names often share the ``KASPI_DELIVERY`` prefix. Prefix matching
    is therefore unsafe: ``KASPI_DELIVERY_CARGO_ASSEMBLY`` is packaging, not
    delivery. Boolean delivery facts and exact normalized aliases have priority.
    """

    _CANCELLED = {"CANCELLED", "CANCELED", "KASPI_DELIVERY_CANCELED", "KASPI_DELIVERY_CANCELLED"}
    _RETURNED = {"RETURN", "RETURNED", "RETURNED_TO_WAREHOUSE"}
    _DELIVERED = {"DELIVERED", "COMPLETED", "KASPI_DELIVERY_DELIVERED"}
    _PICKUP = {"PICKUP", "READY_FOR_PICKUP", "ARRIVED_AT_PICKUP"}
    _SHIPPING = {
        "SHIPPING",
        "TRANSIT",
        "TRANSMITTED",
        "DELIVERY_IN_PROGRESS",
        "KASPI_DELIVERY_CARGO_TRANSMITTED",
        "KASPI_DELIVERY_TRANSMITTED_TO_COURIER",
    }
    _HANDOVER = {
        "HANDOVER",
        "READY_FOR_HANDOVER",
        "WAIT_FOR_COURIER",
        "ASSEMBLED",
        "TRANSFER",
        "KASPI_DELIVERY_WAIT_FOR_COURIER",
    }
    _ASSEMBLY = {
        "ASSEMBLY",
        "PACKING",
        "PACKAGING",
        "KASPI_DELIVERY_CARGO_ASSEMBLY",
        "CARGO_ASSEMBLY",
    }
    _PREORDER = {"PREORDER", "PRE_ORDER"}

    @classmethod
    def decide(cls, facts: OrderDecisionFacts) -> CommerceOrderStage:
        snapshot_tokens = cls._snapshot_tokens(facts)

        if snapshot_tokens & cls._CANCELLED:
            return CommerceOrderStage.CANCELLED
        if facts.returned_to_warehouse is True or snapshot_tokens & cls._RETURNED:
            return CommerceOrderStage.RETURNED
        if snapshot_tokens & cls._DELIVERED:
            return CommerceOrderStage.DELIVERED
        if facts.arrived_at_pickup is True or snapshot_tokens & cls._PICKUP:
            return CommerceOrderStage.PICKUP

        # Explicit courier handover is the strongest live-delivery signal.
        if facts.transmitted_to_courier is True or snapshot_tokens & cls._SHIPPING:
            return CommerceOrderStage.SHIPPING

        # A packed order that has not yet been transmitted waits for Kaspi logistics.
        if facts.assembled is True and facts.transmitted_to_courier is not True:
            return CommerceOrderStage.HANDOVER
        if snapshot_tokens & cls._HANDOVER:
            return CommerceOrderStage.HANDOVER

        if snapshot_tokens & cls._ASSEMBLY:
            return CommerceOrderStage.ASSEMBLY

        if facts.has_lines and facts.all_procurement_received:
            return CommerceOrderStage.ASSEMBLY
        if facts.has_procurement_in_progress:
            return CommerceOrderStage.PREORDER
        if snapshot_tokens & cls._PREORDER:
            return CommerceOrderStage.PREORDER

        raw = cls._normalize(facts.marketplace_status)
        if raw in {"accepted", "accepted_by_merchant"}:
            return CommerceOrderStage.PREORDER
        if raw == "new":
            return CommerceOrderStage.NEW
        if raw:
            return CommerceOrderStage.ACCEPTED
        return CommerceOrderStage.UNKNOWN

    @classmethod
    def source(cls, facts: OrderDecisionFacts) -> str:
        if cls._snapshot_tokens(facts) or any(
            value is not None
            for value in (
                facts.assembled,
                facts.transmitted_to_courier,
                facts.arrived_at_pickup,
                facts.returned_to_warehouse,
            )
        ):
            return "snapshot"
        if facts.has_procurement_in_progress or facts.all_procurement_received:
            return "procurement"
        return "marketplace_order"

    @classmethod
    def _snapshot_tokens(cls, facts: OrderDecisionFacts) -> set[str]:
        return {
            token
            for token in (
                cls._token(facts.snapshot_stage),
                cls._token(facts.snapshot_state),
                cls._token(facts.snapshot_status),
            )
            if token
        }

    @staticmethod
    def _token(value: str | None) -> str:
        return (value or "").strip().upper()

    @staticmethod
    def _normalize(value: str | None) -> str:
        return (value or "").strip().lower()
