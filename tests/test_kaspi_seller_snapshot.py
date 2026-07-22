from backend.app.kaspi_seller import map_seller_order_snapshot


def _live_job_payload() -> dict:
    return {
        "id": 277,
        "status": "succeeded",
        "result": {
            "merchant_id": "11843018",
            "order_code": "1006480798",
            "schema_version": "kaspi-seller-graphql-v1",
            "details_response": {
                "data": {
                    "merchant": {
                        "id": "11843018",
                        "orderDetail": {
                            "code": "1006480798",
                            "creationTime": "2026-07-21T13:14:31.039Z",
                            "modificationTime": "2026-07-21T15:18:17.587Z",
                            "state": "KASPI_DELIVERY_WAIT_FOR_COURIER",
                            "status": "ASSEMBLED",
                            "preOrder": False,
                            "customer": {
                                "firstName": "Дарья",
                                "lastName": "И",
                            },
                            "recipient": None,
                            "delivery": {
                                "actualDeliveryDate": "2026-07-24T14:00:00.000Z",
                                "assembleDate": None,
                                "isOrderArrived": True,
                                "isReturnedToWarehouse": False,
                                "kdAssembled": True,
                                "kdTransmittedToCourier": False,
                                "mode": "DELIVERY_PICKUP",
                                "plannedDeliveryDate": "2026-07-24T14:00:00.000Z",
                                "plannedPointDeliveryDate": None,
                                "transmissionPlanningDate": "2026-07-22T15:00:00.000Z",
                            },
                            "entries": [
                                {
                                    "entryId": 0,
                                    "merchantProduct": {
                                        "barcode": None,
                                        "code": "161785903_585774217",
                                        "name": "GLS Pharmaceuticals Мультивитамины Гамми",
                                    },
                                    "product": {
                                        "code": "161785903",
                                        "name": "GLS Pharmaceuticals Мультивитамины Гамми",
                                    },
                                    "quantity": 1,
                                    "totalPrice": 7700,
                                }
                            ],
                            "markers": [
                                {
                                    "creationTime": "2026-07-21T13:14:31.038Z",
                                    "marker": "APPROVED_BY_BANK",
                                },
                                {
                                    "creationTime": "2026-07-21T15:17:52.390Z",
                                    "marker": "CARGO_ASSEMBLED",
                                },
                            ],
                            "orderSteps": [
                                {
                                    "__typename": "SimpleOrderStep",
                                    "actualTime": "2026-07-21T13:14:31.038Z",
                                    "plannedTime": None,
                                    "step": "APPROVAL",
                                    "timeoutTime": "2026-07-21T13:14:31.038Z",
                                },
                                {
                                    "__typename": "SimpleOrderStep",
                                    "actualTime": None,
                                    "plannedTime": "2026-07-22T15:00:00.000Z",
                                    "step": "TRANSMISSION",
                                    "timeoutTime": "2026-07-28T02:00:00.000Z",
                                },
                                {
                                    "__typename": "RangeOrderStep",
                                    "from": "2026-07-24T14:00:00.000Z",
                                    "step": "PICKUP",
                                    "to": "2026-07-27T14:00:00.000Z",
                                },
                            ],
                            "warehouse": {
                                "name": "041600",
                                "city": {
                                    "id": "196220100",
                                    "name": "Талгар",
                                },
                                "kaspiDelivery": {
                                    "pickupType": "SELF_DELIVERY",
                                },
                            },
                        },
                    }
                }
            },
        },
    }


def test_maps_complete_browser_job_into_domain_snapshot() -> None:
    snapshot = map_seller_order_snapshot(_live_job_payload())

    assert snapshot.merchant_id == "11843018"
    assert snapshot.order_code == "1006480798"
    assert snapshot.state == "KASPI_DELIVERY_WAIT_FOR_COURIER"
    assert snapshot.status == "ASSEMBLED"
    assert snapshot.stage == "HANDOVER"
    assert snapshot.schema_version == "kaspi-seller-graphql-v1"
    assert snapshot.customer_name == "Дарья И"
    assert snapshot.recipient_name is None

    assert snapshot.delivery.assembled is True
    assert snapshot.delivery.transmitted_to_courier is False
    assert snapshot.delivery.order_arrived is True
    assert snapshot.delivery.transmission_planned_at == "2026-07-22T15:00:00.000Z"

    assert snapshot.warehouse is not None
    assert snapshot.warehouse.name == "041600"
    assert snapshot.warehouse.city_name == "Талгар"
    assert snapshot.warehouse.pickup_type == "SELF_DELIVERY"

    assert snapshot.total_quantity == 1
    assert snapshot.total_price == 7700
    assert snapshot.lines[0].merchant_sku == "161785903_585774217"
    assert snapshot.lines[0].product_code == "161785903"
    assert snapshot.marker_names == ("APPROVED_BY_BANK", "CARGO_ASSEMBLED")


def test_preserves_simple_and_range_order_steps() -> None:
    snapshot = map_seller_order_snapshot(_live_job_payload())

    transmission = next(step for step in snapshot.steps if step.step == "TRANSMISSION")
    assert transmission.typename == "SimpleOrderStep"
    assert transmission.actual_time is None
    assert transmission.planned_time == "2026-07-22T15:00:00.000Z"

    pickup = next(step for step in snapshot.steps if step.step == "PICKUP")
    assert pickup.typename == "RangeOrderStep"
    assert pickup.range_from == "2026-07-24T14:00:00.000Z"
    assert pickup.range_to == "2026-07-27T14:00:00.000Z"


def test_preserves_all_order_lines_instead_of_flattening_to_one_sku() -> None:
    payload = _live_job_payload()
    entries = payload["result"]["details_response"]["data"]["merchant"]["orderDetail"]["entries"]
    entries.append(
        {
            "entryId": 1,
            "merchantProduct": {"code": "SECOND_SKU", "name": "Второй товар"},
            "product": {"code": "SECOND_PRODUCT", "name": "Второй товар"},
            "quantity": 2,
            "totalPrice": 5000,
        }
    )

    snapshot = map_seller_order_snapshot(payload)

    assert len(snapshot.lines) == 2
    assert snapshot.total_quantity == 3
    assert snapshot.total_price == 12700
    assert snapshot.lines[1].merchant_sku == "SECOND_SKU"
