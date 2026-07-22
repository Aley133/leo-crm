from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum


class BrowserAgentJobType(StrEnum):
    SUPPLIER_PRODUCT_OBSERVATION = "supplier_product_observation"


@dataclass(frozen=True, slots=True)
class BrowserAgentJobEnvelope:
    job_type: BrowserAgentJobType
    payload: dict[str, object]


def decode_browser_agent_job(*, supplier_product_id: int, url: str, monitor_target_id: int | None) -> BrowserAgentJobEnvelope:
    return BrowserAgentJobEnvelope(
        job_type=BrowserAgentJobType.SUPPLIER_PRODUCT_OBSERVATION,
        payload={
            "supplier_product_id": supplier_product_id,
            "monitor_target_id": monitor_target_id,
            "url": url,
        },
    )


def serialize_claim_payload(envelope: BrowserAgentJobEnvelope) -> dict[str, object]:
    return {
        "job_type": envelope.job_type.value,
        "payload": json.loads(json.dumps(envelope.payload, ensure_ascii=False)),
    }
