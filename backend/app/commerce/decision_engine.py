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
    """Resolve one authoritative CRM business stage from all available facts."""

    @classmethod
    def decide(cls, facts: OrderDecisionFacts) -> CommerceOrderStage:
        tokens = cls._tokens(facts)

        if cls._contains(tokens, "CANCELLED", "CANCELED"):
            return CommerceOrderStage.CANCELLED
        if facts.returned_to_warehouse or cls._contains(tokens, "RETURN", "RETURNED"):
            return CommerceOrderStage.RETURNED
        if cls._contains(tokens, "DELIVERED", "COMPLETED"):
            return CommerceOrderStage.DELIVERED

        if facts.arrived_at_pickup or cls._contains(tokens, "PICKUP", "READY_FOR_PICKUP"):
            return CommerceOrderStage.PICKUP
        if facts.transmitted_to_courier is True or cls._contains(
            tokens,
            "SHIPPING",
            "TRANSIT",
            "TRANSMITTED",
            "KASPI_DELIVERY",
            "DELIVERY_IN_PROGRESS",
        ):
            return CommerceOrderStage.SHIPPING
        if facts.assembled is True and facts.transmitted_to_courier is not True:
            return CommerceOrderStage.HANDOVER
        if cls._contains(
            tokens,
            "HANDOVER",
            "READY_FOR_HANDOVER",
            "WAIT_FOR_COURIER",
            "ASSEMBLED",
            "TRANSFER",
        ):
            return CommerceOrderStage.HANDOVER
        if cls._contains(tokens, "ASSEMBLY", "PACKING", "PACKAGING"):
            return CommerceOrderStage.ASSEMBLY

        if facts.has_lines and facts.all_procurement_received:
            return CommerceOrderStage.ASSEMBLY
        if facts.has_procurement_in_progress:
            return CommerceOrderStage.PREORDER
        if cls._contains(tokens, "PREORDER", "PRE_ORDER"):
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
        snapshot_tokens = {
            cls._token(facts.snapshot_stage),
            cls._token(facts.snapshot_state),
            cls._token(facts.snapshot_status),
        }
        snapshot_tokens.discard("")
        known_snapshot = cls._contains(
            snapshot_tokens,
            "CANCELLED",
            "CANCELED",
            "RETURN",
            "DELIVERED",
            "COMPLETED",
            "PICKUP",
            "SHIPPING",
            "TRANSIT",
            "TRANSMITTED",
            "KASPI_DELIVERY",
            "ASSEMBLY",
            "PACKING",
            "PACKAGING",
            "HANDOVER",
            "TRANSFER",
            "WAIT_FOR_COURIER",
            "ASSEMBLED",
            "PREORDER",
            "PRE_ORDER",
        )
        if known_snapshot or any(
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
    def _tokens(cls, facts: OrderDecisionFacts) -> set[str]:
        return {
            token
            for token in (
                cls._token(facts.snapshot_stage),
                cls._token(facts.snapshot_state),
                cls._token(facts.snapshot_status),
                cls._token(facts.marketplace_status),
            )
            if token
        }

    @staticmethod
    def _contains(tokens: set[str], *needles: str) -> bool:
        return any(any(needle in token for needle in needles) for token in tokens)

    @staticmethod
    def _token(value: str | None) -> str:
        return (value or "").strip().upper()

    @staticmethod
    def _normalize(value: str | None) -> str:
        return (value or "").strip().lower()
