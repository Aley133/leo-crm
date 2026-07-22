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
    """Resolve one authoritative CRM stage through a strict source cascade.

    Recognized Kaspi Seller facts override the Marketplace API. When Snapshot is
    absent or unknown, the normalized Marketplace status remains authoritative.
    Procurement can only refine an early accepted order into PREORDER or ASSEMBLY;
    it can never rewrite physical, terminal, or delivery stages.
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

    _MARKETPLACE_STAGES = {
        "new": CommerceOrderStage.NEW,
        "accepted": CommerceOrderStage.PREORDER,
        "accepted_by_merchant": CommerceOrderStage.PREORDER,
        "preorder": CommerceOrderStage.PREORDER,
        "pre_order": CommerceOrderStage.PREORDER,
        "received": CommerceOrderStage.RECEIVED,
        "assembly": CommerceOrderStage.ASSEMBLY,
        "packing": CommerceOrderStage.ASSEMBLY,
        "packaging": CommerceOrderStage.ASSEMBLY,
        "handover": CommerceOrderStage.HANDOVER,
        "transfer": CommerceOrderStage.HANDOVER,
        "shipping": CommerceOrderStage.SHIPPING,
        "transit": CommerceOrderStage.SHIPPING,
        "pickup": CommerceOrderStage.PICKUP,
        "delivered": CommerceOrderStage.DELIVERED,
        "completed": CommerceOrderStage.DELIVERED,
        "cancelled": CommerceOrderStage.CANCELLED,
        "canceled": CommerceOrderStage.CANCELLED,
        "returned": CommerceOrderStage.RETURNED,
        "return": CommerceOrderStage.RETURNED,
        "unknown": CommerceOrderStage.UNKNOWN,
    }

    @classmethod
    def decide(cls, facts: OrderDecisionFacts) -> CommerceOrderStage:
        snapshot_stage = cls._snapshot_stage(facts)
        if snapshot_stage is not None:
            return snapshot_stage

        marketplace_stage = cls._marketplace_stage(facts.marketplace_status)

        # Physical and terminal Marketplace stages are never downgraded by an
        # unfinished or stale purchase request.
        if marketplace_stage in {
            CommerceOrderStage.ASSEMBLY,
            CommerceOrderStage.HANDOVER,
            CommerceOrderStage.SHIPPING,
            CommerceOrderStage.PICKUP,
            CommerceOrderStage.DELIVERED,
            CommerceOrderStage.CANCELLED,
            CommerceOrderStage.RETURNED,
        }:
            return marketplace_stage

        # Procurement only refines early lifecycle stages.
        if facts.has_lines and facts.all_procurement_received:
            return CommerceOrderStage.ASSEMBLY
        if facts.has_procurement_in_progress:
            return CommerceOrderStage.PREORDER

        if marketplace_stage is not None:
            return marketplace_stage
        return CommerceOrderStage.UNKNOWN

    @classmethod
    def source(cls, facts: OrderDecisionFacts) -> str:
        if cls._snapshot_stage(facts) is not None:
            return "snapshot"
        if cls._marketplace_stage(facts.marketplace_status) is not None:
            return "marketplace_order"
        if facts.has_procurement_in_progress or facts.all_procurement_received:
            return "procurement"
        return "marketplace_order"

    @classmethod
    def _snapshot_stage(cls, facts: OrderDecisionFacts) -> CommerceOrderStage | None:
        tokens = cls._snapshot_tokens(facts)

        if tokens & cls._CANCELLED:
            return CommerceOrderStage.CANCELLED
        if facts.returned_to_warehouse is True or tokens & cls._RETURNED:
            return CommerceOrderStage.RETURNED
        if tokens & cls._DELIVERED:
            return CommerceOrderStage.DELIVERED
        if facts.arrived_at_pickup is True or tokens & cls._PICKUP:
            return CommerceOrderStage.PICKUP
        if facts.transmitted_to_courier is True or tokens & cls._SHIPPING:
            return CommerceOrderStage.SHIPPING
        if facts.assembled is True and facts.transmitted_to_courier is not True:
            return CommerceOrderStage.HANDOVER
        if tokens & cls._HANDOVER:
            return CommerceOrderStage.HANDOVER
        if tokens & cls._ASSEMBLY:
            return CommerceOrderStage.ASSEMBLY
        if tokens & cls._PREORDER:
            return CommerceOrderStage.PREORDER
        return None

    @classmethod
    def _marketplace_stage(cls, value: str | None) -> CommerceOrderStage | None:
        raw = cls._normalize(value)
        if not raw:
            return None
        return cls._MARKETPLACE_STAGES.get(raw, CommerceOrderStage.ACCEPTED)

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
