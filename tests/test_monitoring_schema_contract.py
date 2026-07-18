import os

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from backend.app.monitoring import (  # noqa: E402
    AttemptOutcome,
    BindingStatus,
    MonitorStatus,
    SourceHealthStatus,
    MonitorTarget,
    SupplierOfferObservation,
    SupplierOfferState,
)
from backend.app.suppliers import ProductBinding  # noqa: E402


def _column_names(model: type) -> set[str]:
    return {column.name for column in model.__table__.columns}


def test_binding_lifecycle_columns_match_contract() -> None:
    assert {
        "status",
        "decision_source",
        "priority",
        "confirmed_at",
        "last_validated_at",
        "last_mismatch_reason",
        "updated_at",
    } <= _column_names(ProductBinding)


def test_monitor_target_contains_stale_worker_protection_fields() -> None:
    assert {"lease_owner", "lease_token", "lease_until", "next_check_at"} <= _column_names(MonitorTarget)
    assert MonitorTarget.__table__.c.lease_token.unique is True


def test_offer_state_and_observation_are_separate_models() -> None:
    state_columns = _column_names(SupplierOfferState)
    observation_columns = _column_names(SupplierOfferObservation)

    assert {"version", "last_checked_at"} <= state_columns
    assert "monitor_attempt_id" in observation_columns
    assert "version" not in observation_columns


def test_status_vocabulary_is_canonical() -> None:
    assert {item.value for item in BindingStatus} == {
        "candidate",
        "confirmed",
        "active",
        "degraded",
        "disabled",
        "rejected",
    }
    assert {item.value for item in MonitorStatus} == {
        "active",
        "paused",
        "degraded",
        "manual_review",
        "disabled",
    }
    assert "captcha" in {item.value for item in AttemptOutcome}
    assert "captcha_required" in {item.value for item in SourceHealthStatus}
