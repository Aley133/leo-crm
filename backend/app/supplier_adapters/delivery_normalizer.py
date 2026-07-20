from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable

KAZAKHSTAN_TZ = timezone(timedelta(hours=5), name="Asia/Almaty")

_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
_DATE_RE = re.compile(
    r"\b([0-3]?\d)\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\b",
    re.I,
)
_DAY_RE = re.compile(r"(?:через\s*)?(\d{1,2})\s*(?:день|дня|дней|дн)\b", re.I)


class DeliveryNormalizer:
    """Normalize visible Russian delivery promises to calendar-day distance.

    The normalizer is marketplace-agnostic and has no database, queue, browser,
    pricing or XML dependencies.
    """

    @classmethod
    def from_context(
        cls,
        text: str,
        *,
        markers: Iterable[str],
        excluded_phrases: Iterable[str] = (),
        window: int = 2,
        now: datetime | None = None,
    ) -> int | None:
        """Parse delivery only from lines surrounding trusted delivery markers."""
        lines = [" ".join(line.split()) for line in str(text or "").splitlines() if line.strip()]
        marker_values = tuple(value.casefold().replace("ё", "е") for value in markers)
        excluded_values = tuple(value.casefold().replace("ё", "е") for value in excluded_phrases)
        selected: list[str] = []

        for index, line in enumerate(lines):
            normalized_line = line.casefold().replace("ё", "е")
            if not any(marker in normalized_line for marker in marker_values):
                continue
            start = max(0, index - window)
            end = min(len(lines), index + window + 1)
            for candidate in lines[start:end]:
                normalized_candidate = candidate.casefold().replace("ё", "е")
                if any(phrase in normalized_candidate for phrase in excluded_values):
                    continue
                selected.append(candidate)

        if not selected:
            return None
        return cls.from_text("\n".join(selected), now=now)

    @classmethod
    def from_text(cls, text: str, *, now: datetime | None = None) -> int | None:
        normalized = " ".join(str(text or "").split()).casefold().replace("ё", "е")
        if not normalized:
            return None

        current = now or datetime.now(KAZAKHSTAN_TZ)
        if current.tzinfo is None:
            current = current.replace(tzinfo=KAZAKHSTAN_TZ)
        else:
            current = current.astimezone(KAZAKHSTAN_TZ)

        # An explicit calendar date is the strongest promise. Marketplace pages
        # may also contain unrelated words such as "tomorrow" in recommendations.
        calendar_days: list[int] = []
        for match in _DATE_RE.finditer(normalized):
            day = int(match.group(1))
            month = _MONTHS[match.group(2).casefold()]
            try:
                candidate = datetime(current.year, month, day, tzinfo=KAZAKHSTAN_TZ)
            except ValueError:
                continue
            if candidate.date() < current.date():
                try:
                    candidate = candidate.replace(year=current.year + 1)
                except ValueError:
                    continue
            delta = (candidate.date() - current.date()).days
            if 0 <= delta <= 30:
                calendar_days.append(delta)
        if calendar_days:
            return min(calendar_days)

        explicit_days = [int(value) for value in _DAY_RE.findall(normalized)]
        if explicit_days:
            return min(value for value in explicit_days if 0 <= value <= 30)

        if "послезавтра" in normalized:
            return 2
        if "завтра" in normalized:
            return 1
        if "сегодня" in normalized:
            return 0
        return None
