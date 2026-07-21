from backend.app.browser_agent_api import BrowserAgentJobCreate
from backend.app.browser_agent_job_contract import (
    BrowserAgentJobType,
    decode_browser_agent_job,
    encode_kaspi_seller_order_job,
)


def test_kaspi_seller_job_round_trips_through_legacy_storage_columns() -> None:
    stored_url = encode_kaspi_seller_order_job(
        merchant_id="11843018",
        order_code="1006480798",
    )

    envelope = decode_browser_agent_job(
        supplier_product_id=0,
        url=stored_url,
        monitor_target_id=None,
    )

    assert envelope.job_type == BrowserAgentJobType.KASPI_SELLER_ORDER_DETAILS
    assert envelope.payload == {
        "merchant_id": "11843018",
        "order_code": "1006480798",
    }


def test_existing_supplier_job_remains_backward_compatible() -> None:
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


def test_kaspi_job_create_requires_merchant_and_order_code() -> None:
    payload = BrowserAgentJobCreate(
        job_type=BrowserAgentJobType.KASPI_SELLER_ORDER_DETAILS,
        payload={"merchant_id": "11843018", "order_code": "1002303844"},
    )

    assert payload.job_type == BrowserAgentJobType.KASPI_SELLER_ORDER_DETAILS
    assert payload.payload == {
        "merchant_id": "11843018",
        "order_code": "1002303844",
    }
