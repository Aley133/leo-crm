from backend.app.kaspi_seller import map_seller_order_facts, resolve_seller_stage


def _graphql_detail(**detail) -> dict:
    return {
        "data": {
            "merchant": {
                "id": "11843018",
                "orderDetail": detail,
            }
        }
    }


def test_verified_seller_preorder_stage() -> None:
    payload = _graphql_detail(
        code="1002303844",
        state="KASPI_DELIVERY_WAIT_FOR_POINT_DELIVERY",
        status="ACCEPTED",
        preOrder=True,
        delivery={
            "isOrderArrived": False,
            "kdAssembled": False,
            "kdTransmittedToCourier": False,
        },
        orderSteps=[{"step": "PRE_ORDER", "actualTime": None}],
    )
    assert resolve_seller_stage(map_seller_order_facts(payload)) == "ACCEPTED_BY_MERCHANT"


def test_verified_seller_packaging_stage() -> None:
    payload = _graphql_detail(
        code="1006563363",
        state="KASPI_DELIVERY_CARGO_ASSEMBLY",
        status="PRE_ORDERED",
        preOrder=True,
        delivery={
            "isOrderArrived": True,
            "kdAssembled": False,
            "kdTransmittedToCourier": False,
        },
        orderSteps=[
            {"step": "PRE_ORDER", "actualTime": "2026-07-21T16:07:04.986Z"}
        ],
    )
    assert resolve_seller_stage(map_seller_order_facts(payload)) == "ASSEMBLY"


def test_verified_seller_handover_stage() -> None:
    payload = _graphql_detail(
        code="1006480798",
        state="KASPI_DELIVERY_WAIT_FOR_COURIER",
        status="ASSEMBLED",
        preOrder=False,
        delivery={
            "isOrderArrived": True,
            "kdAssembled": True,
            "kdTransmittedToCourier": False,
        },
        markers=[{"marker": "CARGO_ASSEMBLED"}],
    )
    assert resolve_seller_stage(map_seller_order_facts(payload)) == "HANDOVER"


def test_verified_seller_shipping_stage() -> None:
    payload = _graphql_detail(
        code="1000772384",
        state="KASPI_DELIVERY_TRANSMITTED",
        status="TRANSMITTED",
        preOrder=True,
        delivery={
            "isOrderArrived": True,
            "kdAssembled": True,
            "kdTransmittedToCourier": True,
        },
        orderSteps=[
            {"step": "TRANSMISSION", "actualTime": "2026-07-21T14:07:39.000Z"}
        ],
    )
    assert resolve_seller_stage(map_seller_order_facts(payload)) == "SHIPPING"


def test_terminal_status_wins_over_stale_delivery_flags() -> None:
    payload = _graphql_detail(
        code="1006187575",
        state="ARCHIVE",
        status="CANCELLED",
        preOrder=True,
        delivery={
            "isOrderArrived": True,
            "kdAssembled": True,
            "kdTransmittedToCourier": True,
        },
    )
    assert resolve_seller_stage(map_seller_order_facts(payload)) == "CANCELLED"
