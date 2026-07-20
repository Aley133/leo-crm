from datetime import datetime

from backend.app.supplier_adapters.wildberries_delivery_aware import WildberriesDeliveryAwareAdapter


def test_wildberries_relative_delivery_promises() -> None:
    parser = WildberriesDeliveryAwareAdapter._delivery_days_from_text

    assert parser("Цена 4 500 ₸\nДоставка завтра\nСклад WB") == 1
    assert parser("Купить сейчас\nПослезавтра, склад WB") == 2
    assert parser("Получение через 3 дня") == 3
    assert parser("Доставка сегодня") == 0


def test_wildberries_calendar_delivery_date(monkeypatch) -> None:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 20, 12, 0, tzinfo=tz)

    import backend.app.supplier_adapters.delivery_normalizer as module

    monkeypatch.setattr(module, "datetime", FixedDateTime)
    parser = WildberriesDeliveryAwareAdapter._delivery_days_from_text

    assert parser("26 июля, склад WB") == 6
    assert parser("Послезавтра в рекомендациях\n26 июля, склад WB") == 6
    assert parser("Получение через 2 дня\n26 июля, склад WB") == 6


def test_wildberries_ignores_unrelated_rating() -> None:
    assert WildberriesDeliveryAwareAdapter._delivery_days_from_text("Эвалар 4,9") is None
