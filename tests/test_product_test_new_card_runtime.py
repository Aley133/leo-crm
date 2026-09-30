import pytest

from tools.product_test_new_card import runtime


def _draft() -> dict:
    return {
        "sku": "900000001",
        "title": "Solgar Test Product",
        "brand": "Solgar",
        "category": "Master - Vitamins",
        "description": "Подробное описание товара по данным производителя. " * 3,
        "weight": "0.25",
        "attributes": [
            {
                "code": "vitamins*country",
                "title": "Страна производства",
                "required": True,
                "value": "США",
            }
        ],
        "images": ["https://ir.ozone.ru/s3/multimedia/new-card.webp"],
    }


def test_official_import_waits_for_finished_and_detailed_success(monkeypatch) -> None:
    calls: list[str] = []

    class FakeApi:
        def __init__(self, token: str) -> None:
            assert token == "secret-token"

        def import_products(self, payload: list[dict]) -> dict:
            calls.append("import")
            assert payload[0]["sku"] == "900000001"
            return {"accepted": True, "status_code": 200, "body": {"code": "import-1"}}

        def import_status(self, code: str) -> dict:
            calls.append("status")
            return {"status": "FINISHED" if calls.count("status") == 2 else "PROCESSING"}

        def import_result(self, code: str) -> dict:
            calls.append("result")
            return {"status": "FINISHED", "errors": 0, "items": [{"status": "SUCCESS"}]}

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(runtime, "OfficialProductsApi", FakeApi)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)

    result = runtime.create_new_card("secret-token", _draft(), attempts=3, poll_seconds=0.5)

    assert result["result"] == "NEW_CARD_ACCEPTED_FOR_MODERATION"
    assert result["detailed_ok"] is True
    assert calls == ["import", "status", "status", "result", "close"]


def test_official_import_never_accepts_failed_detailed_result(monkeypatch) -> None:
    class FakeApi:
        def __init__(self, _token: str) -> None:
            pass

        def import_products(self, _payload: list[dict]) -> dict:
            return {"accepted": True, "status_code": 200, "body": {"code": "import-2"}}

        def import_status(self, _code: str) -> dict:
            return {"status": "FINISHED"}

        def import_result(self, _code: str) -> dict:
            return {
                "status": "FINISHED",
                "errors": "1",
                "items": [{"status": "REJECTED", "message": "required attribute"}],
            }

        def close(self) -> None:
            pass

    monkeypatch.setattr(runtime, "OfficialProductsApi", FakeApi)

    with pytest.raises(runtime.NewCardImportRejected, match="detailed validation") as caught:
        runtime.create_new_card("secret-token", _draft(), attempts=1)

    assert "required attribute" in str(caught.value)
    assert "SKU 900000001" in str(caught.value)
    assert "import import-2" in str(caught.value)


def test_detailed_result_surfaces_nested_attribute_errors() -> None:
    ok, error_count, failed = runtime._detailed_outcome(
        {
            "errors": 1,
            "warnings": 0,
            "result": {
                "4671307561": {
                    "state": "ERRORS",
                    "errors": [
                        {
                            "attributeCode": "Dietary supplements*Main component",
                            "message": "Значение характеристики не найдено в справочнике",
                        },
                        {"images": ["Не удалось скачать изображение"]},
                    ],
                }
            },
        }
    )

    assert ok is False
    assert error_count == 1
    assert any(
        "SKU 4671307561: Dietary supplements*Main component: Значение характеристики"
        in row
        for row in failed
    )
    assert any("SKU 4671307561: images: Не удалось скачать изображение" in row for row in failed)


def test_detailed_result_explains_when_kaspi_only_returns_errors_state() -> None:
    ok, error_count, failed = runtime._detailed_outcome(
        {"errors": 1, "result": {"4671307561": {"state": "ERRORS"}}}
    )

    assert ok is False
    assert error_count == 1
    assert failed == [
        "SKU 4671307561: ERRORS; Kaspi не вернул текст причины в detailed result"
    ]
