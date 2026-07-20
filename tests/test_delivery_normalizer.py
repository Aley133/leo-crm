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


def test_ozon_context_ignores_promotion_countdown() -> None:
    now = datetime(2026, 7, 20, 12, 0, tzinfo=KZ)
    text = """
    Распродажа
    10 дней до конца
    3772 единицы осталось
    2632 ₸
    В корзину
    Доставим завтра
    Доставка и возврат
    Пункт Ozon: Бокина, 18
    """

    assert DeliveryNormalizer.from_context(
        text,
        markers=("доставим", "доставка", "пункт ozon"),
        excluded_phrases=("до конца", "распродажа", "осталось"),
        window=2,
        now=now,
    ) == 1


def test_ozon_context_handles_flattened_browser_text() -> None:
    now = datetime(2026, 7, 20, 12, 0, tzinfo=KZ)
    text = (
        "Распродажа 3772 единицы осталось 10 дней до конца 2632 ₸ "
        "В корзину Доставим завтра У других продавцов от 5061 ₸ "
        "Доставка и возврат Бокина, 18 Со склада Ozon"
    )

    assert DeliveryNormalizer.from_context(
        text,
        markers=("доставим", "доставка", "пункт ozon"),
        excluded_phrases=("до конца", "распродажа", "осталось"),
        now=now,
    ) == 1


def test_ozon_adapter_waits_and_reads_full_delivery_text() -> None:
    source = open(
        "backend/app/supplier_adapters/ozon_browser_access.py",
        encoding="utf-8",
    ).read()

    assert 'code = "ozon-browser-v9"' in source
    assert "DeliveryNormalizer.from_context(" in source
    assert 'wait_for_function(' in source
    assert 'page.mouse.wheel(0, 500)' in source
    assert 'page.locator("body").inner_text(timeout=10000)' in source
    assert 'metadata["delivery_source"] = "ozon_waited_full_visible_text"' in source
    assert 'visible_delivery_context_normalized' not in source
