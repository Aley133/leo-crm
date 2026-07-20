from datetime import datetime, timedelta, timezone

from backend.app.supplier_adapters.delivery_normalizer import DeliveryNormalizer


KZ = timezone(timedelta(hours=5))


def test_delivery_normalizer_prioritizes_explicit_calendar_date() -> None:
    now = datetime(2026, 7, 20, 12, 0, tzinfo=KZ)
    text = "Послезавтра в рекомендациях. Получение 26 июля, склад WB."

    assert DeliveryNormalizer.from_text(text, now=now) == 6


def test_delivery_normalizer_handles_relative_promises() -> None:
    now = datetime(2026, 7, 20, 12, 0, tzinfo=KZ)

    assert DeliveryNormalizer.from_text("Доставка сегодня", now=now) == 0
    assert DeliveryNormalizer.from_text("Получите завтра", now=now) == 1
    assert DeliveryNormalizer.from_text("Привезём послезавтра", now=now) == 2
    assert DeliveryNormalizer.from_text("Получение через 4 дня", now=now) == 4


def test_delivery_normalizer_does_not_require_system_tzdata() -> None:
    now = datetime(2026, 7, 20, 12, 0)

    assert DeliveryNormalizer.from_text("Доставка 26 июля", now=now) == 6


def test_ozon_adapter_uses_shared_delivery_normalizer() -> None:
    source = open(
        "backend/app/supplier_adapters/ozon_browser_access.py",
        encoding="utf-8",
    ).read()

    assert "DeliveryNormalizer.from_text(response.body_text)" in source
    assert 'metadata["delivery_source"] = "visible_text_normalized"' in source
