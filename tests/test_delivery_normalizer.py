from datetime import datetime, timedelta, timezone

from backend.app.supplier_adapters.delivery_normalizer import DeliveryNormalizer
from backend.app.supplier_adapters.ozon_delivery import OzonDeliveryExtractor


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


def test_ozon_delivery_extractor_ignores_installment_today() -> None:
    now = datetime(2026, 7, 21, 0, 5, tzinfo=KZ)
    text = (
        "Оригинальный товар Оплата после получения При заказе в пункт выдачи "
        "2632 ₸ 658 ₸ × 4 месяца 0 ₸ сегодня В корзину Доставим завтра "
        "Доставка и возврат Бокина, 18 Со склада Ozon"
    )

    assert OzonDeliveryExtractor.from_text(text, now=now) == 1


def test_ozon_delivery_extractor_ignores_promotion_countdown() -> None:
    now = datetime(2026, 7, 20, 12, 0, tzinfo=KZ)
    text = (
        "Распродажа 3772 единицы осталось 10 дней до конца 2632 ₸ "
        "В корзину Доставим завтра У других продавцов от 5061 ₸ "
        "Доставка и возврат Бокина, 18 Со склада Ozon"
    )

    assert OzonDeliveryExtractor.from_text(text, now=now) == 1


def test_ozon_delivery_candidates_prioritize_current_product_widget() -> None:
    now = datetime(2026, 7, 21, 0, 5, tzinfo=KZ)

    assert OzonDeliveryExtractor.from_candidates(
        ["В корзину\nДоставим завтра"],
        fallback_text="0 ₸ сегодня. Рекомендации: Послезавтра",
        now=now,
    ) == 1


def test_ozon_delivery_candidate_accepts_compact_current_product_promise() -> None:
    now = datetime(2026, 7, 21, 0, 5, tzinfo=KZ)

    assert OzonDeliveryExtractor.from_candidates(["Послезавтра"], now=now) == 2
    assert OzonDeliveryExtractor.from_candidates(["В корзину\n26 июля"], now=now) == 5


def test_ozon_full_page_does_not_use_weak_context_today() -> None:
    now = datetime(2026, 7, 21, 0, 5, tzinfo=KZ)
    text = "Пункт выдачи 2632 ₸ 0 ₸ сегодня В корзину рекомендации послезавтра"

    assert OzonDeliveryExtractor.from_text(text, now=now) is None


def test_ozon_adapter_waits_reads_and_records_delivery_evidence() -> None:
    source = open(
        "backend/app/supplier_adapters/ozon_browser_access.py",
        encoding="utf-8",
    ).read()

    assert 'code = "ozon-browser-v12"' in source
    assert "OzonDeliveryExtractor.from_candidates(" in source
    assert 'document.querySelectorAll(\'[data-widget="webAddToCart"]\')' in source
    assert 'metadata["delivery_source"] = "ozon_current_product_dom"' in source
    assert 'metadata["base_delivery_days"]' in source
    assert 'metadata["semantic_delivery_days"]' in source
    assert 'metadata["delivery_candidates"]' in source
    assert 'metadata["delivery_context"]' in source
