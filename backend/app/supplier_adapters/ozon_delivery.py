from __future__ import annotations

import re
from datetime import datetime

from .delivery_normalizer import DeliveryNormalizer


class OzonDeliveryExtractor:
    """Extract an Ozon delivery promise before generic day normalization.

    Ozon pages mix real delivery promises with unrelated commercial text such as
    ``0 ₸ сегодня`` (installment payment) and promotion countdowns. This class is
    intentionally marketplace-specific: it selects only text adjacent to trusted
    Ozon delivery controls, then delegates calendar arithmetic to DeliveryNormalizer.
    """

    # Direct fulfillment verbs are the strongest signal and must be evaluated before
    # weaker location/context labels such as "пункт выдачи" or "в корзину".
    _PRIMARY = (
        r"доставим|доставят|доставить|привезем|привезут|получите|"
        r"получить|заберите|забрать"
    )
    _SECONDARY = (
        r"доставка|получение|курьером(?:\s+ozon)?|курьер(?:\s+ozon)?|"
        r"пункт(?:ы)?\s+выдачи|постамат(?:ы)?|самовывоз|в\s+корзину"
    )
    _CONTEXT = rf"{_PRIMARY}|{_SECONDARY}"
    _RELATIVE = r"сегодня|завтра|послезавтра"
    _MONTH = (
        r"января|февраля|марта|апреля|мая|июня|июля|августа|"
        r"сентября|октября|ноября|декабря"
    )

    @classmethod
    def from_text(cls, text: str, *, now: datetime | None = None) -> int | None:
        raw = str(text or "").replace("\xa0", " ")
        normalized = re.sub(r"[ \t\r\f\v]+", " ", raw).casefold().replace("ё", "е")
        if not normalized:
            return None

        # Payment/installment labels are not delivery promises. Remove them before any
        # relative-day parsing so "0 ₸ сегодня" can never outrank "Доставим завтра".
        normalized = re.sub(
            r"\b\d[\d\s.,]*\s*(?:₸|тг|тенге)\s*(?:за\s*\d+\s*(?:шт|ед)\s*)?сегодня\b",
            " ",
            normalized,
            flags=re.IGNORECASE,
        )

        # Strongest signal: a calendar date adjacent to a direct delivery verb.
        primary_date_patterns = (
            rf"(?:{cls._PRIMARY})[^\n]{{0,100}}?\b\d{{1,2}}\s+(?:{cls._MONTH})\b",
            rf"\b\d{{1,2}}\s+(?:{cls._MONTH})\b[^\n]{{0,70}}?(?:{cls._PRIMARY})",
            rf"(?:{cls._PRIMARY})[^\n]{{0,100}}?\b\d{{1,2}}[./-]\d{{1,2}}(?:[./-]\d{{2,4}})?\b",
        )
        for pattern in primary_date_patterns:
            match = re.search(pattern, normalized, flags=re.IGNORECASE | re.DOTALL)
            if match:
                days = DeliveryNormalizer.from_text(match.group(0), now=now)
                if days is not None:
                    return days

        # Direct relative promises such as "Доставим завтра" have absolute priority.
        primary_relative_patterns = (
            rf"(?:{cls._PRIMARY})[^\n]{{0,60}}?\b(?:{cls._RELATIVE})\b",
            rf"\b(?:{cls._RELATIVE})\b[^\n]{{0,40}}?(?:{cls._PRIMARY})",
        )
        for pattern in primary_relative_patterns:
            match = re.search(pattern, normalized, flags=re.IGNORECASE | re.DOTALL)
            if match:
                days = DeliveryNormalizer.from_text(match.group(0), now=now)
                if days is not None:
                    return days

        # Fall back to broader delivery context only when no direct promise exists.
        secondary_date_patterns = (
            rf"(?:{cls._SECONDARY})[^\n]{{0,100}}?\b\d{{1,2}}\s+(?:{cls._MONTH})\b",
            rf"\b\d{{1,2}}\s+(?:{cls._MONTH})\b[^\n]{{0,70}}?(?:{cls._SECONDARY})",
        )
        for pattern in secondary_date_patterns:
            match = re.search(pattern, normalized, flags=re.IGNORECASE | re.DOTALL)
            if match:
                days = DeliveryNormalizer.from_text(match.group(0), now=now)
                if days is not None:
                    return days

        secondary_relative_patterns = (
            rf"(?:{cls._SECONDARY})[^\n]{{0,70}}?\b(?:{cls._RELATIVE})\b",
            rf"\b(?:{cls._RELATIVE})\b[^\n]{{0,50}}?(?:{cls._SECONDARY})",
        )
        for pattern in secondary_relative_patterns:
            match = re.search(pattern, normalized, flags=re.IGNORECASE | re.DOTALL)
            if match:
                days = DeliveryNormalizer.from_text(match.group(0), now=now)
                if days is not None:
                    return days

        # Finally accept an explicit day count only inside direct delivery context first,
        # then broader delivery context.
        for context in (cls._PRIMARY, cls._SECONDARY):
            count_pattern = (
                rf"(?:{context})[^\n]{{0,100}}?(?:через\s*)?"
                rf"\d{{1,2}}\s*(?:день|дня|дней|дн)\b"
            )
            match = re.search(count_pattern, normalized, flags=re.IGNORECASE | re.DOTALL)
            if match:
                return DeliveryNormalizer.from_text(match.group(0), now=now)

        return None
