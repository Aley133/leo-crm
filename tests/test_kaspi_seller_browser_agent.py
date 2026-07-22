from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from backend.app.kaspi_seller.graphql import GET_ORDER_DETAILS_QUERY
from tools.browser_agent import _run_job
from tools.kaspi_seller_browser import (
    KaspiSellerBrowserAdapter,
    KaspiSellerOrderRequest,
)


class FakeKaspiAdapter:
    async def fetch_order(self, request: KaspiSellerOrderRequest):
        assert request.merchant_id == "11843018"
        assert request.order_code == "1006480798"
        return {
            "schema_version": "kaspi-seller-graphql-v1",
            "merchant_id": request.merchant_id,
            "order_code": request.order_code,
            "state": "KASPI_DELIVERY_WAIT_FOR_COURIER",
            "status": "ASSEMBLED",
        }


def test_agent_routes_kaspi_seller_job_by_job_type() -> None:
    result = asyncio.run(
        _run_job(
            {
                "job_type": "kaspi_seller_order_details",
                "payload": {
                    "merchant_id": "11843018",
                    "order_code": "1006480798",
                },
            },
            {"kaspi_seller": FakeKaspiAdapter()},
        )
    )

    assert result["schema_version"] == "kaspi-seller-graphql-v1"
    assert result["state"] == "KASPI_DELIVERY_WAIT_FOR_COURIER"
    assert result["status"] == "ASSEMBLED"


class FakePage:
    def __init__(self) -> None:
        self.url = "https://kaspi.kz/mc/#/orders"
        self.operations: list[dict] = []

    async def goto(self, url: str, **kwargs) -> None:
        assert url == "https://kaspi.kz/mc/#/orders"
        assert kwargs["wait_until"] == "domcontentloaded"

    async def evaluate(self, script: str, args: dict):
        assert 'credentials: "include"' in script
        self.operations.append(args)
        detail = {
            "code": "1006480798",
            "state": "KASPI_DELIVERY_WAIT_FOR_COURIER",
            "status": "ASSEMBLED",
            "preOrder": False,
            "delivery": {
                "isOrderArrived": True,
                "kdAssembled": True,
                "kdTransmittedToCourier": False,
            },
            "markers": [],
            "orderSteps": [],
        }
        if args["operationName"] == "getOrderState":
            detail = {"state": detail["state"]}
        return {
            "ok": True,
            "status": 200,
            "finalUrl": args["url"],
            "contentType": "application/json",
            "body": {
                "data": {
                    "merchant": {
                        "id": "11843018",
                        "orderDetail": detail,
                    }
                }
            },
            "text": "",
        }


class FakePool:
    def __init__(self) -> None:
        self.page = FakePage()

    @asynccontextmanager
    async def isolated_page(self):
        yield self.page


def test_browser_adapter_executes_state_and_details_graphql() -> None:
    pool = FakePool()
    adapter = KaspiSellerBrowserAdapter(pool)  # type: ignore[arg-type]

    result = asyncio.run(
        adapter.fetch_order(
            KaspiSellerOrderRequest(
                merchant_id="11843018",
                order_code="1006480798",
            )
        )
    )

    assert [item["operationName"] for item in pool.page.operations] == [
        "getOrderState",
        "getOrderDetails",
    ]
    assert pool.page.operations[0]["variables"] == {
        "merchantUid": "11843018",
        "orderCode": "1006480798",
    }
    assert pool.page.operations[1]["variables"] == {
        "merchantUid": "11843018",
        "orderCode": "1006480798",
        "skipCustomerPhone": True,
    }
    assert result["state"] == "KASPI_DELIVERY_WAIT_FOR_COURIER"
    assert result["status"] == "ASSEMBLED"
    assert result["details_response"]["data"]["merchant"]["orderDetail"]["delivery"][
        "kdAssembled"
    ] is True


def test_details_query_uses_live_union_and_phone_skip_contract() -> None:
    assert "$skipCustomerPhone: Boolean! = false" in GET_ORDER_DETAILS_QUERY
    assert "phoneNumber @skip(if: $skipCustomerPhone)" in GET_ORDER_DETAILS_QUERY
    assert "... on SimpleOrderStep" in GET_ORDER_DETAILS_QUERY
    assert "... on RangeOrderStep" in GET_ORDER_DETAILS_QUERY
    assert "orderSteps {\n        step" not in GET_ORDER_DETAILS_QUERY
