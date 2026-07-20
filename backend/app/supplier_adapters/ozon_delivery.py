from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable

from .delivery_normalizer import DeliveryNormalizer


class OzonDeliveryExtractor:
    """Extract delivery only from trusted Ozon product-card semantics.

    Ozon mixes delivery text with installment labels (``0 ₸ сегодня``),
    promotion countdowns and recommendation cards. The browser adapter must
    pass the current product's ``webAddToCart`` text as the first candidates.
    Generic page text is only a last-resort fallback and requires an explicit
    delivery verb.
    """

    _PRIMARY = (
        r"доставим|доставят|доставить|привезем|привезут|получите|"
        r"получить|заберите|забрать"
    )
    _RELATIVE = r"сегодня|завтра|послезавтра"
    _MONTH = (
        r"января|февраля|марта|апреля|мая|июня|июля|августа|"
        r"сентября|октября|ноября|декабря"
    )
    _DATE = rf"\d{{1,2}}\s+(?:{_MONTH})"
    _NUMERIC_DATE = r"\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?"
    _DAY_COUNT = r"(?:через\s*)?\d{1,2}\s*(?:день|дня|дней|дн)\b"

    @classmethod
    def from_candidates(
        cls,
        candidates: Iterable[str],
        *,
        fallback_text: str = "",
        now: datetime | None = None,
    ) -> int | None:
        """Parse ordered DOM evidence, then use strict page-text fallback.

        Candidate order is significant. The browser adapter puts
        ``[data-widget='webAddToCart']`` first, so recommendation-card promises
        cannot override the current product.
        """
        seen: set[str] = set()
        for candidate in candidates:
            normalized = cls._normalize(candidate)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            days = cls._from_trusted_candidate(normalized, now=now)
            if days is not None:
                return days

        return cls.from_text(fallback_text, now=now)

    @classmethod
    def from_text(cls, text: str, *, now: datetime | None = None) -> int | None:
        """Strict fallback for flattened page text.

        Only an explicit fulfillment verb may authorize a relative date, a
        calendar date or a day count. Weak words such as ``пункт выдачи`` and
        ``в корзину`` are intentionally insufficient on a full page because
        they occur next to installment and recommendation text.
        """
        normalized = cls._sanitize(text)
        if not normalized:
            return None

        patterns = (
            rf"(?:{cls._PRIMARY})[^\n]{{0,100}}?\b(?:{cls._DATE}|{cls._NUMERIC_DATE})\b",
            rf"\b(?:{cls._DATE}|{cls._NUMERIC_DATE})\b[^\n]{{0,70}}?(?:{cls._PRIMARY})",
            rf"(?:{cls._PRIMARY})[^\n]{{0,60}}?\b(?:{cls._RELATIVE})\b",
            rf"\b(?:{cls._RELATIVE})\b[^\n]{{0,40}}?(?:{cls._PRIMARY})",
            rf"(?:{cls._PRIMARY})[^\n]{{0,100}}?{cls._DAY_COUNT}",
        )
        for pattern in patterns:
            match = re.search(pattern, normalized, flags=re.IGNORECASE | re.DOTALL)
            if not match:
                continue
            days = DeliveryNormalizer.from_text(match.group(0), now=now)
            if days is not None:
                return days
        return None

    @classmethod
    def _from_trusted_candidate(
        cls,
        normalized: str,
        *,
        now: datetime | None = None,
    ) -> int | None:
        # Current-product DOM evidence is already scoped, but installment text
        # can still be embedded in the same widget/container.
        normalized = cls._sanitize(normalized)
        if not normalized:
            return None

        # Prefer explicit delivery phrases when available.
        strict = cls.from_text(normalized, now=now)
        if strict is not None:
            return strict

        # webAddToCart can expose only the promise value, e.g. "Послезавтра".
        exact_relative = re.fullmatch(
            r"(?:в\s+корзину\s+)?({})".format(cls._RELATIVE),
            normalized,
        )
        if exact_relative:
            return DeliveryNormalizer.from_text(exact_relative.group(1), now=now)

        # Or a compact phrase without a fulfillment verb, e.g. "В корзину 26 июля".
        compact_patterns = (
            rf"(?:в\s+корзину\s+)?\b({cls._DATE}|{cls._NUMERIC_DATE})\b",
            rf"(?:в\s+корзину\s+)?\b({cls._DAY_COUNT})",
        )
        for pattern in compact_patterns:
            match = re.search(pattern, normalized, flags=re.IGNORECASE)
            if match:
                return DeliveryNormalizer.from_text(match.group(1), now=now)
        return None

    @classmethod
    def _sanitize(cls, text: str) -> str:
        normalized = cls._normalize(text)
        if not normalized:
            return ""

        # Remove installment/payment labels before looking for relative days.
        normalized = re.sub(
            r"\b\d[\d\s.,]*\s*(?:₸|тг|тенге)\s*"
            r"(?:за\s*\d+\s*(?:шт|ед)\s*)?сегодня\b",
            " ",
            normalized,
            flags=re.IGNORECASE,
        )
        return re.sub(r"\s+", " ", normalized).strip()

    @staticmethod
    def _normalize(text: str) -> str:
        return (
            re.sub(r"[ \t\r\f\v]+", " ", str(text or "").replace("\xa0", " "))
            .casefold()
            .replace("ё", "е")
            .strip()
        )
