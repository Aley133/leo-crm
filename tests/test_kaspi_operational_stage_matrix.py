from backend.app.commerce.decision_engine import (
    CommerceOrderStage,
    OrderDecisionEngine,
    OrderDecisionFacts,
)


def decide(**kwargs) -> CommerceOrderStage:
    return OrderDecisionEngine.decide(
        OrderDecisionFacts(marketplace_status="accepted", **kwargs)
    )


def test_kaspi_cargo_assembly_is_packaging_not_shipping() -> None:
    assert decide(
        snapshot_state="KASPI_DELIVERY_CARGO_ASSEMBLY",
        snapshot_status="ACCEPTED_BY_MERCHANT",
        assembled=False,
        transmitted_to_courier=False,
    ) == CommerceOrderStage.ASSEMBLY


def test_kaspi_wait_for_courier_is_handover() -> None:
    assert decide(
        snapshot_state="KASPI_DELIVERY_WAIT_FOR_COURIER",
        snapshot_status="ASSEMBLED",
        assembled=True,
        transmitted_to_courier=False,
    ) == CommerceOrderStage.HANDOVER


def test_transmitted_to_courier_is_shipping() -> None:
    assert decide(
        snapshot_state="KASPI_DELIVERY_WAIT_FOR_COURIER",
        snapshot_status="ASSEMBLED",
        assembled=True,
        transmitted_to_courier=True,
    ) == CommerceOrderStage.SHIPPING


def test_kaspi_delivery_cancelled_is_cancelled() -> None:
    assert decide(
        snapshot_state="KASPI_DELIVERY_CANCELED",
        snapshot_status="CANCELED",
    ) == CommerceOrderStage.CANCELLED


def test_accepted_order_without_logistics_facts_stays_preorder() -> None:
    assert decide(
        snapshot_status="ACCEPTED_BY_MERCHANT",
        assembled=False,
        transmitted_to_courier=False,
        has_procurement_in_progress=True,
    ) == CommerceOrderStage.PREORDER
