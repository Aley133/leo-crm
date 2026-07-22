from datetime import UTC, datetime
from decimal import Decimal

import pytest

from backend.app.commerce.domain import CommerceOrder, CommerceOrderStage


def _order(snapshot_stage: str) -> CommerceOrder:
    return CommerceOrder(
        order_id=1,
        external_code="996801988",
        marketplace="kaspi",
        status="accepted",
        currency="KZT",
        total_amount=Decimal("10000"),
        ordered_at=datetime(2026, 7, 22, tzinfo=UTC),
        delivered_at=None,
        lines=(),
        snapshot_stage=snapshot_stage,
        snapshot_observed_at=datetime(2026, 7, 22, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("snapshot_stage", "expected"),
    [
        ("PREORDER", CommerceOrderStage.PREORDER),
        ("PRE_ORDER", CommerceOrderStage.PREORDER),
        ("ASSEMBLY", CommerceOrderStage.ASSEMBLY),
        ("PACKING", CommerceOrderStage.ASSEMBLY),
        ("PACKAGING", CommerceOrderStage.ASSEMBLY),
        ("HANDOVER", CommerceOrderStage.HANDOVER),
        ("READY_FOR_HANDOVER", CommerceOrderStage.HANDOVER),
        ("TRANSFER", CommerceOrderStage.HANDOVER),
        ("CANCELLED", CommerceOrderStage.CANCELLED),
        ("CANCELED", CommerceOrderStage.CANCELLED),
    ],
)
def test_kaspi_snapshot_stage_is_displayed_as_crm_operational_stage(
    snapshot_stage: str,
    expected: CommerceOrderStage,
) -> None:
    order = _order(snapshot_stage)

    assert order.stage == expected
    assert order.stage_source == "snapshot"


def test_cancelled_snapshot_removes_revenue_and_procurement() -> None:
    order = _order("CANCELLED")

    assert order.recognized_revenue == Decimal("0")
    assert order.procurement_required_lines == 0
