import json
from datetime import datetime, timezone

from backend.app.kaspi_seller.snapshot_models import KaspiSellerOrderSnapshotRecord
from backend.app.kaspi_seller.timeline_api import snapshot_record_payload, timeline_event_payload
from backend.app.kaspi_seller.timeline_models import KaspiSellerOrderTimelineEvent


def test_timeline_event_payload_exposes_business_transition() -> None:
    event = KaspiSellerOrderTimelineEvent(
        id=7,
        snapshot_id=12,
        previous_snapshot_id=11,
        merchant_id="11843018",
        order_code="1006480798",
        event_type="ORDER_TRANSFERRED",
        from_stage="HANDOVER",
        to_stage="SHIPPING",
        event_payload=json.dumps({"state": "KASPI_DELIVERY_TRANSMITTED"}),
        occurred_at=datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
    )

    payload = timeline_event_payload(event)

    assert payload["event_type"] == "ORDER_TRANSFERRED"
    assert payload["from_stage"] == "HANDOVER"
    assert payload["to_stage"] == "SHIPPING"
    assert payload["details"] == {"state": "KASPI_DELIVERY_TRANSMITTED"}


def test_latest_snapshot_payload_keeps_normalized_order() -> None:
    observed_at = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
    record = KaspiSellerOrderSnapshotRecord(
        id=12,
        browser_agent_job_id=99,
        previous_snapshot_id=11,
        merchant_id="11843018",
        order_code="1006480798",
        state="KASPI_DELIVERY_TRANSMITTED",
        status="TRANSMITTED",
        stage="SHIPPING",
        schema_version="kaspi-seller-graphql-v1",
        snapshot_fingerprint="a" * 64,
        changed=True,
        snapshot_payload=json.dumps({"order_code": "1006480798", "stage": "SHIPPING"}),
        observed_at=observed_at,
    )

    payload = snapshot_record_payload(record)

    assert payload["order_code"] == "1006480798"
    assert payload["stage"] == "SHIPPING"
    assert payload["snapshot"] == {"order_code": "1006480798", "stage": "SHIPPING"}
    assert payload["observed_at"] == observed_at
