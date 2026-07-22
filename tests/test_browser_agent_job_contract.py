from backend.app.browser_agent_api import BrowserAgentJobCreate
from backend.app.browser_agent_job_contract import (
    BrowserAgentJobType,
    decode_browser_agent_job,
)


def test_supplier_job_round_trips_through_storage_columns() -> None:
    envelope = decode_browser_agent_job(
        supplier_product_id=42,
        url="https://www.ozon.ru/product/example-42/",
        monitor_target_id=7,
    )

    assert envelope.job_type == BrowserAgentJobType.SUPPLIER_PRODUCT_OBSERVATION
    assert envelope.payload == {
        "supplier_product_id": 42,
        "monitor_target_id": 7,
        "url": "https://www.ozon.ru/product/example-42/",
    }


def test_browser_agent_job_create_requires_supplier_identity() -> None:
    payload = BrowserAgentJobCreate(
        job_type=BrowserAgentJobType.SUPPLIER_PRODUCT_OBSERVATION,
        supplier_product_id=42,
        monitor_target_id=7,
        url="https://www.wildberries.ru/catalog/42/detail.aspx",
    )

    assert payload.job_type == BrowserAgentJobType.SUPPLIER_PRODUCT_OBSERVATION
    assert payload.supplier_product_id == 42
    assert payload.monitor_target_id == 7
