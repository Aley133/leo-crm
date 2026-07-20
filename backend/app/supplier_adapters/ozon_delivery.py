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

    _CONTEXT = (
        r"доставим|доставка|доставят|доставить|получите|получить|получение|"
        r"курьером(?:\s+ozon)?|курьер(?:\s+ozon)?|пункт(?:ы)?\s+выдачи|"
        r"постамат(?:ы)?|самовывоз|в\s+корзину"
    )
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

        # Strongest signal: a calendar date adjacent to an Ozon delivery marker.
        date_patterns = (
            rf"(?:{cls._CONTEXT})[^\n]{{0,140}}?\b\d{{1,2}}\s+(?:{cls._MONTH})\b",
            rf"\b\d{{1,2}}\s+(?:{cls._MONTH})\b[^\n]{{0,100}}?(?:{cls._CONTEXT})",
            rf"(?:{cls._CONTEXT})[^\n]{{0,140}}?\b\d{{1,2}}[./-]\d{{1,2}}(?:[./-]\d{{2,4}})?\b",
        )
        for pattern in date_patterns:
            match = re.search(pattern, normalized, flags=re.IGNORECASE | re.DOTALL)
            if match:
                days = DeliveryNormalizer.from_text(match.group(0), now=now)
                if days is not None:
                    return days

        # Then relative promises, but only when attached to delivery semantics.
        relative_patterns = (
            rf"(?:{cls._CONTEXT})[^\n]{{0,120}}?\b(?:{cls._RELATIVE})\b",
            rf"\b(?:{cls._RELATIVE})\b[^\n]{{0,80}}?(?:{cls._CONTEXT})",
        )
        for pattern in relative_patterns:
            match = re.search(pattern, normalized, flags=re.IGNORECASE | re.DOTALL)
            if match:
                days = DeliveryNormalizer.from_text(match.group(0), now=now)
                if days is not None:
                    return days

        # Finally accept an explicit day count only inside delivery context.
        count_pattern = rf"(?:{cls._CONTEXT})[^\n]{{0,120}}?(?:через\s*)?\d{{1,2}}\s*(?:день|дня|дней|дн)\b"
        match = re.search(count_pattern, normalized, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return DeliveryNormalizer.from_text(match.group(0), now=now)

        return None
