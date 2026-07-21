from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, urlencode, urlparse


class BrowserAgentJobType(StrEnum):
    SUPPLIER_PRODUCT_OBSERVATION = "supplier_product_observation"
    KASPI_SELLER_ORDER_DETAILS = "kaspi_seller_order_details"


_KASPI_SCHEME = "leo-job"
_KASPI_HOST = BrowserAgentJobType.KASPI_SELLER_ORDER_DETAILS.value


@dataclass(frozen=True, slots=True)
class BrowserAgentJobEnvelope:
    job_type: BrowserAgentJobType
    payload: dict[str, object]


def encode_kaspi_seller_order_job(*, merchant_id: str, order_code: str) -> str:
    merchant = merchant_id.strip()
    code = order_code.strip()
    if not merchant:
        raise ValueError("merchant_id is required")
    if not code:
        raise ValueError("order_code is required")
    return f"{_KASPI_SCHEME}://{_KASPI_HOST}?{urlencode({'merchant_id': merchant, 'order_code': code})}"


def decode_browser_agent_job(*, supplier_product_id: int, url: str, monitor_target_id: int | None) -> BrowserAgentJobEnvelope:
    parsed = urlparse(url)
    if parsed.scheme == _KASPI_SCHEME and parsed.netloc == _KASPI_HOST:
        query = parse_qs(parsed.query)
        merchant_id = (query.get("merchant_id") or [""])[0].strip()
        order_code = (query.get("order_code") or [""])[0].strip()
        if not merchant_id or not order_code:
            raise ValueError("invalid Kaspi Seller browser job payload")
        return BrowserAgentJobEnvelope(
            job_type=BrowserAgentJobType.KASPI_SELLER_ORDER_DETAILS,
            payload={"merchant_id": merchant_id, "order_code": order_code},
        )

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
