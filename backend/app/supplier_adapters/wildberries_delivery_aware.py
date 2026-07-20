from __future__ import annotations

from .delivery_normalizer import DeliveryNormalizer
from .wildberries_browser_access import WildberriesBrowserAccessAdapter


class WildberriesDeliveryAwareAdapter(WildberriesBrowserAccessAdapter):
    """WB browser adapter with marketplace-neutral delivery normalization."""

    code = "wildberries-browser-verified-v9"

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

        # Relative promises may be rendered as standalone lines near purchase
        # controls, while explicit dates still win inside DeliveryNormalizer.
        for line in lines:
            low = line.casefold().replace("ё", "е")
            if any(word in low for word in ("сегодня", "завтра", "послезавтра")):
                selected.append(line)

        return DeliveryNormalizer.from_text("\n".join(selected))
