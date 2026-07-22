from datetime import datetime, timezone

from backend.app.kaspi_seller.snapshot_models import KaspiSellerOrderSnapshotRecord
from backend.app.kaspi_seller.snapshot_storage import persist_kaspi_seller_snapshot


class _ScalarList:
    def __init__(self, values):
        self._values = list(values)

    def all(self):
        return self._values


class FakeSession:
    def __init__(self, scalar_results):
        self.scalar_results = list(scalar_results)
        self.added = []
        self.records = {
            item.id: item
            for item in self.scalar_results
            if isinstance(item, KaspiSellerOrderSnapshotRecord) and item.id is not None
        }

    def scalar(self, statement):
        result = self.scalar_results.pop(0)
        if isinstance(result, KaspiSellerOrderSnapshotRecord) and result.id is not None:
            self.records[result.id] = result
        return result

    def scalars(self, statement):
        return _ScalarList([])

    def get(self, model, record_id):
        return self.records.get(record_id)

    def add(self, record):
        self.added.append(record)

    def flush(self):
        record = self.added[-1]
        record.id = 101 + len(self.added) - 1
        if isinstance(record, KaspiSellerOrderSnapshotRecord):
            self.records[record.id] = record


def _payload(stage: str = "HANDOVER") -> dict:
    return {
        "schema_version": "kaspi-seller-graphql-v1",
        "merchant_id": "11843018",
        "order_code": "1006480798",
        "snapshot": {
            "merchant_id": "11843018",
            "order_code": "1006480798",
            "state": "KASPI_DELIVERY_WAIT_FOR_COURIER",
            "status": "ASSEMBLED",
            "stage": stage,
            "schema_version": "kaspi-seller-graphql-v1",
            "preorder": False,
            "lines": [],
            "steps": [],
            "markers": [],
        },
    }


def test_first_snapshot_is_persisted_as_changed() -> None:
    db = FakeSession([None, None])
    observed_at = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)

    result = persist_kaspi_seller_snapshot(
        db,  # type: ignore[arg-type]
        browser_agent_job_id=77,
        payload=_payload(),
        observed_at=observed_at,
    )

    assert result.snapshot_id == 101
    assert result.changed is True
    assert result.previous_snapshot_id is None
    record = db.added[0]
    assert record.browser_agent_job_id == 77
    assert record.order_code == "1006480798"
    assert record.stage == "HANDOVER"
    assert record.observed_at == observed_at
    assert len(record.snapshot_fingerprint) == 64
    assert len(result.timeline_event_ids) == 1


def test_unchanged_snapshot_links_to_previous_observation() -> None:
    previous = KaspiSellerOrderSnapshotRecord(
        id=55,
        browser_agent_job_id=76,
        previous_snapshot_id=None,
        merchant_id="11843018",
        order_code="1006480798",
        state="KASPI_DELIVERY_WAIT_FOR_COURIER",
        status="ASSEMBLED",
        stage="HANDOVER",
        schema_version="kaspi-seller-graphql-v1",
        snapshot_fingerprint="placeholder",
        changed=True,
        snapshot_payload="{}",
        observed_at=datetime(2026, 7, 22, 9, 0, tzinfo=timezone.utc),
    )
    first = FakeSession([None, None])
    baseline = persist_kaspi_seller_snapshot(
        first,  # type: ignore[arg-type]
        browser_agent_job_id=76,
        payload=_payload(),
        observed_at=previous.observed_at,
    )
    previous.snapshot_fingerprint = first.added[0].snapshot_fingerprint

    db = FakeSession([None, previous])
    result = persist_kaspi_seller_snapshot(
        db,  # type: ignore[arg-type]
        browser_agent_job_id=77,
        payload=_payload(),
        observed_at=datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc),
    )

    assert baseline.changed is True
    assert result.changed is False
    assert result.previous_snapshot_id == 55
    assert db.added[0].previous_snapshot_id == 55
    assert result.timeline_event_ids == ()


def test_same_browser_job_completion_is_idempotent() -> None:
    existing = KaspiSellerOrderSnapshotRecord(
        id=88,
        browser_agent_job_id=77,
        previous_snapshot_id=55,
        merchant_id="11843018",
        order_code="1006480798",
        state="KASPI_DELIVERY_WAIT_FOR_COURIER",
        status="ASSEMBLED",
        stage="HANDOVER",
        schema_version="kaspi-seller-graphql-v1",
        snapshot_fingerprint="abc",
        changed=False,
        snapshot_payload="{}",
        observed_at=datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc),
    )
    db = FakeSession([existing])

    result = persist_kaspi_seller_snapshot(
        db,  # type: ignore[arg-type]
        browser_agent_job_id=77,
        payload=_payload(),
        observed_at=existing.observed_at,
    )

    assert result.snapshot_id == 88
    assert result.changed is False
    assert result.previous_snapshot_id == 55
    assert result.timeline_event_ids == ()
    assert db.added == []