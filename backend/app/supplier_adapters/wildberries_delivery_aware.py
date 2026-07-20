from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .wildberries_browser_access import WildberriesBrowserAccessAdapter

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


class WildberriesDeliveryAwareAdapter(WildberriesBrowserAccessAdapter):
    """WB browser adapter with Kazakhstan-aware delivery date parsing.

    This class changes only normalization of the visible delivery promise. It
    keeps the parent adapter's browser verification and application boundary.
    """

    code = "wildberries-browser-verified-v8"

    @classmethod
    def _delivery_days_from_text(cls, body_text: str) -> int | None:
        lines = [" ".join(line.split()) for line in body_text.splitlines() if line.strip()]
        context_words = (
            "достав",
            "получ",
            "пункт",
            "пвз",
            "постамат",
            "курьер",
            "забрать",
            "самовывоз",
            "привез",
            "склад wb",
            "склад",
        )
        selected: list[str] = []
        for index, line in enumerate(lines):
            low = line.casefold().replace("ё", "е")
            if any(word in low for word in context_words):
                selected.extend(lines[max(0, index - 3) : index + 5])

        # Relative promises may be rendered as a standalone line near the buy
        # controls, so include them even when WB omits the word "delivery".
        for line in lines:
            low = line.casefold().replace("ё", "е")
            if any(word in low for word in ("сегодня", "завтра", "послезавтра")):
                selected.append(line)

        text = " ".join(selected).casefold().replace("ё", "е")
        if not text:
            return None

        # An explicit calendar promise is authoritative. WB pages can contain
        # unrelated relative words in recommendations or secondary widgets.
        calendar_days = cls._calendar_delivery_days(text)
        if calendar_days is not None:
            return calendar_days

        values = [int(match) for match in _DAY_RE.findall(text)]
        if values:
            return min(values)

        if "послезавтра" in text:
            return 2
        if "завтра" in text:
            return 1
        if "сегодня" in text:
            return 0
        return None

    @classmethod
    def _calendar_delivery_days(cls, text: str) -> int | None:
        now = datetime.now(ZoneInfo("Asia/Almaty"))
        candidates: list[int] = []
        for match in _DATE_RE.finditer(text):
            day = int(match.group(1))
            month = _MONTHS[match.group(2).casefold()]
            try:
                candidate = now.replace(
                    month=month,
                    day=day,
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                )
            except ValueError:
                continue
            if candidate.date() < now.date():
                try:
                    candidate = candidate.replace(year=now.year + 1)
                except ValueError:
                    continue
            delta = (candidate.date() - now.date()).days
            if 0 <= delta <= 30:
                candidates.append(delta)
        return min(candidates) if candidates else None
