from __future__ import annotations

import asyncio
import html
import json
import random
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://kaspi.kz",
    "Referer": "https://kaspi.kz/shop/",
}
KASPI_TIMEZONE = ZoneInfo("Asia/Almaty")


@dataclass(frozen=True, slots=True)
class KaspiCompetitorSnapshot:
    product_name: str
    product_brand: str | None
    own_price_kzt: Decimal | None
    competitor_price_kzt: Decimal | None
    competitor_name: str | None
    own_position: int | None
    seller_count: int
    product_url: str
    own_delivery: str | None = None
    competitor_delivery: str | None = None
    offers: tuple[dict[str, Any], ...] = ()
    page_visible_price_kzt: Decimal | None = None
    market_context_ok: bool = False
    market_context_reason: str | None = None
    own_delivery_days: int | None = None
    competitor_delivery_days: int | None = None
    delivery_filtered_count: int = 0
    delivery_selection_reason: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryAssessment:
    own_delivery_days: int | None
    competitor_delivery_days: int | None
    price_gap_kzt: Decimal | None
    delivery_gap_days: int | None
    ignored: bool
    reason: str | None = None


def _slugify(value: str) -> str:
    table = str.maketrans({
        "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z",
        "и":"i","й":"i","к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r",
        "с":"s","т":"t","у":"u","ф":"f","х":"h","ц":"ts","ч":"ch","ш":"sh",
        "щ":"sch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
    })
    rendered = html.unescape(value).lower().translate(table)
    return re.sub(r"[^0-9a-z]+", "-", rendered).strip("-") or "product"


def _extract_balanced(text: str, start: int) -> str | None:
    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _promo_conditions(page_html: str) -> dict[str, Any] | None:
    marker = '"promoConditions":'
    position = page_html.find(marker)
    while position >= 0:
        brace = page_html.find("{", position + len(marker))
        if brace < 0:
            return None
        raw = _extract_balanced(page_html, brace)
        if raw:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = None
            if isinstance(value, dict) and ("categoryCodes" in value or "baseProductCodes" in value):
                return value
        position = page_html.find(marker, position + len(marker))
    return None


def _page_title(page_html: str) -> str | None:
    patterns = (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        r'<title[^>]*>(.*?)</title>',
    )
    for pattern in patterns:
        match = re.search(pattern, page_html, flags=re.I | re.S)
        if match:
            value = re.sub(r"\s+", " ", html.unescape(match.group(1))).strip()
            value = re.sub(r"\s*[|—-]\s*Kaspi(?:\.kz)?\s*$", "", value, flags=re.I).strip()
            # Kaspi public titles are often rendered as
            #   "Купить <exact model> в <city> – Магазин на Kaspi.kz".
            # The merchant pricefeed/process payload expects the product model,
            # not the SEO title.  Passing the full SEO title can yield HTTP 200
            # with an operation id while the asynchronous operation is ignored.
            value = re.sub(r"^Купить\s+", "", value, flags=re.I).strip()
            value = re.sub(
                r"\s+в\s+[^–—|]+?\s*[–—|]\s*Магазин\s+на\s+Kaspi(?:\.kz)?\s*$",
                "",
                value,
                flags=re.I,
            ).strip()
            value = re.sub(
                r"\s*[–—|]\s*Магазин\s+на\s+Kaspi(?:\.kz)?\s*$",
                "",
                value,
                flags=re.I,
            ).strip()
            if value:
                return value
    return None


def _to_decimal_price(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    text = str(raw).replace("\u00a0", " ").replace(" ", "").replace(",", ".").strip()
    try:
        value = Decimal(text)
    except Exception:
        return None
    if value <= 0:
        return None
    return value


def _page_visible_price(page_html: str) -> Decimal | None:
    """Extract the headline/lowest visible price from the public product page.

    This is deliberately conservative. We only use structured price markers or
    a visible 'Цена ... ₸' fragment. We do NOT take an arbitrary JSON `price`
    because the page contains promo/bonus/old prices as well.
    """
    structured_patterns = (
        r'<meta[^>]+property=["\']product:price:amount["\'][^>]+content=["\']([0-9 .]+)',
        r'<meta[^>]+content=["\']([0-9 .]+)["\'][^>]+property=["\']product:price:amount["\']',
        r'<meta[^>]+itemprop=["\']price["\'][^>]+content=["\']([0-9 .]+)',
        r'<meta[^>]+content=["\']([0-9 .]+)["\'][^>]+itemprop=["\']price["\']',
        r'"lowPrice"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)',
    )
    for pattern in structured_patterns:
        match = re.search(pattern, page_html, flags=re.I | re.S)
        if match:
            value = _to_decimal_price(match.group(1))
            if value is not None:
                return value

    # Kaspi's rendered page commonly contains a compact price block. Restrict
    # the search to the word "Цена" plus a tenge sign so installment values do
    # not become the market headline price.
    visible_patterns = (
        r'Цена.{0,180}?([0-9][0-9\s\u00a0]{1,12})\s*₸',
        r'price[^>]{0,80}>\s*([0-9][0-9\s\u00a0]{1,12})\s*₸',
    )
    for pattern in visible_patterns:
        match = re.search(pattern, page_html, flags=re.I | re.S)
        if match:
            value = _to_decimal_price(match.group(1))
            if value is not None:
                return value
    return None


def _offers(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("offers", "data", "items", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            nested = _offers(value)
            if nested:
                return nested
    return []


def _delivery_summary(offer: dict[str, Any] | None) -> str | None:
    if not offer:
        return None
    parts: list[str] = []
    for key, value in offer.items():
        lowered = str(key).lower()
        if "delivery" not in lowered and "pickup" not in lowered:
            continue
        if isinstance(value, (str, int, float, bool)) and str(value).strip():
            parts.append(f"{key}={value}")
        elif isinstance(value, dict):
            for nested_key, nested_value in value.items():
                if isinstance(nested_value, (str, int, float, bool)) and str(nested_value).strip():
                    parts.append(f"{key}.{nested_key}={nested_value}")
        if len(parts) >= 4:
            break
    return "; ".join(parts[:4]) or None


def _kaspi_today() -> date:
    return datetime.now(KASPI_TIMEZONE).date()


def _delivery_value_days(value: Any, *, today: date) -> int | None:
    """Normalize one explicit delivery value to calendar days from today."""

    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            numeric = float(value)
            days = int(numeric)
        except (TypeError, ValueError, OverflowError):
            return None
        if numeric == days and 0 <= days <= 60:
            return days
        # Some Offers API variants serialize the promised delivery date as a
        # Unix timestamp instead of ISO-8601. Accept seconds and milliseconds,
        # while keeping small numeric values reserved for explicit durations.
        timestamp = numeric / 1000 if numeric >= 100_000_000_000 else numeric
        if timestamp >= 1_000_000_000:
            try:
                delivery_date = datetime.fromtimestamp(
                    timestamp, tz=KASPI_TIMEZONE
                ).date()
            except (OSError, OverflowError, ValueError):
                return None
            distance = (delivery_date - today).days
            return distance if 0 <= distance <= 60 else None
        return None

    rendered = str(value).strip()
    lowered = rendered.casefold()
    if not rendered:
        return None
    if "послезавтра" in lowered:
        return 2
    if "завтра" in lowered:
        return 1
    if "сегодня" in lowered:
        return 0

    if re.fullmatch(r"\d{10}|\d{13}", rendered):
        return _delivery_value_days(int(rendered), today=today)

    day_match = re.search(
        r"(?<!\d)(\d{1,2})(?:\s*[-–—]\s*(\d{1,2}))?\s*(?:дн(?:я|ей)?|день)",
        lowered,
    )
    if day_match:
        # For a range use the earliest promised day. A competitor is ignored
        # only when even its fastest stated delivery is sufficiently slower.
        days = int(day_match.group(1))
        return days if 0 <= days <= 60 else None

    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(rendered.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        delivery_date = (
            parsed.astimezone(KASPI_TIMEZONE).date()
            if parsed.tzinfo is not None
            else parsed.date()
        )
        days = (delivery_date - today).days
        return days if 0 <= days <= 60 else None

    date_match = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", rendered)
    if date_match:
        try:
            delivery_date = date(
                int(date_match.group(3)),
                int(date_match.group(2)),
                int(date_match.group(1)),
            )
        except ValueError:
            return None
        days = (delivery_date - today).days
        return days if 0 <= days <= 60 else None
    return None


def _delivery_days(offer: dict[str, Any] | None, *, today: date | None = None) -> int | None:
    """Return buyer-facing delivery time without confusing it with pickup.

    Kaspi currently returns an ISO timestamp in the top-level `delivery` field.
    Explicit delivery date/duration variants are accepted as fallbacks. Boolean
    flags and intermediate `deliverySteps` values are deliberately ignored.
    """

    if not offer:
        return None
    reference = today or _kaspi_today()
    candidates: list[tuple[int, int]] = []

    def visit(value: Any, *, parent_is_delivery: bool = False, depth: int = 0) -> None:
        if depth > 3 or not isinstance(value, dict):
            return
        for raw_key, nested in value.items():
            key = re.sub(r"[^a-zа-я0-9]", "", str(raw_key).casefold())
            is_delivery = key == "delivery"
            is_explicit_detail = (
                "delivery" in key
                and any(
                    token in key
                    for token in ("date", "time", "deadline", "duration", "day", "term", "period", "text")
                )
            ) or (
                parent_is_delivery
                and any(
                    token in key
                    for token in ("date", "time", "deadline", "duration", "day", "term", "period", "text")
                )
            )
            if isinstance(nested, dict):
                visit(
                    nested,
                    parent_is_delivery=is_delivery or is_explicit_detail,
                    depth=depth + 1,
                )
                continue
            if not is_delivery and not is_explicit_detail:
                continue
            days = _delivery_value_days(nested, today=reference)
            if days is not None:
                candidates.append((0 if is_delivery else 1, days))

    visit(offer)
    if not candidates:
        return None
    best_priority = min(priority for priority, _days in candidates)
    return min(days for priority, days in candidates if priority == best_priority)


def _assess_delivery_advantage(
    own: dict[str, Any] | None,
    competitor: dict[str, Any],
    *,
    max_price_premium_kzt: Decimal | int | str,
    min_delivery_advantage_days: int,
    today: date | None = None,
) -> DeliveryAssessment:
    """Decide whether a slower, slightly cheaper offer should be ignored."""

    own_price = None if own is None else _offer_price(own)
    competitor_price = _offer_price(competitor)
    own_days = _delivery_days(own, today=today)
    competitor_days = _delivery_days(competitor, today=today)
    price_gap = (
        None
        if own_price is None or competitor_price is None
        else own_price - competitor_price
    )
    delivery_gap = (
        None
        if own_days is None or competitor_days is None
        else competitor_days - own_days
    )
    premium = max(Decimal("0"), Decimal(str(max_price_premium_kzt)))
    advantage = max(1, int(min_delivery_advantage_days))
    ignored = bool(
        price_gap is not None
        and Decimal("0") <= price_gap <= premium
        and delivery_gap is not None
        and delivery_gap >= advantage
    )
    price_gap_text = "—" if price_gap is None else format(price_gap, "f")
    premium_text = format(premium, "f")
    if ignored:
        reason = (
            f"Исключён из ценового ориентира: наша доставка быстрее на "
            f"{delivery_gap} дн., а разница цены {price_gap_text} ₸ "
            f"не превышает порог {premium_text} ₸."
        )
    elif own_price is None or competitor_price is None:
        reason = "Защита доставки не применена: цена одной из сторон не распознана."
    elif own_days is None:
        reason = "Защита доставки не применена: срок нашей доставки не распознан."
    elif competitor_days is None:
        reason = "Защита доставки не применена: срок доставки конкурента не распознан."
    elif price_gap is not None and price_gap < 0:
        reason = "Защита доставки не нужна: наша цена уже ниже цены конкурента."
    elif price_gap is not None and price_gap > premium:
        reason = (
            f"Выбран: разница цены {price_gap_text} ₸ превышает допустимую "
            f"доплату {premium_text} ₸."
        )
    elif delivery_gap is not None and delivery_gap <= 0:
        reason = "Выбран: конкурент доставляет не позже нашей строки."
    else:
        reason = (
            f"Выбран: наша доставка быстрее только на {delivery_gap} дн., "
            f"что меньше порога {advantage} дн."
        )
    return DeliveryAssessment(
        own_delivery_days=own_days,
        competitor_delivery_days=competitor_days,
        price_gap_kzt=price_gap,
        delivery_gap_days=delivery_gap,
        ignored=ignored,
        reason=reason,
    )


def _select_delivery_aware_competitor(
    own: dict[str, Any] | None,
    competitors: list[dict[str, Any]],
    *,
    max_price_premium_kzt: Decimal | int | str,
    min_delivery_advantage_days: int,
    today: date | None = None,
) -> tuple[dict[str, Any] | None, dict[int, DeliveryAssessment]]:
    assessments: dict[int, DeliveryAssessment] = {}
    selected: dict[str, Any] | None = None
    ignored_before_selection = False
    own_price = None if own is None else _offer_price(own)
    for candidate in competitors:
        assessment = _assess_delivery_advantage(
            own,
            candidate,
            max_price_premium_kzt=max_price_premium_kzt,
            min_delivery_advantage_days=min_delivery_advantage_days,
            today=today,
        )
        assessments[id(candidate)] = assessment
        if selected is not None:
            continue
        if assessment.ignored:
            ignored_before_selection = True
            continue
        candidate_price = _offer_price(candidate)
        if (
            ignored_before_selection
            and own_price is not None
            and candidate_price is not None
            and candidate_price >= own_price
        ):
            # A slower cheap offer sets the maximum premium we are currently
            # willing to defend. Do not use a later expensive offer to raise
            # our price beyond that already-validated premium.
            continue
        selected = candidate
    return selected, assessments


def _offer_price(offer: dict[str, Any]) -> Decimal | None:
    return _to_decimal_price(offer.get("price"))


def _merchant_id(offer: dict[str, Any]) -> str:
    direct = (
        offer.get("merchantId")
        or offer.get("merchantUID")
        or offer.get("merchantUid")
    )
    if direct not in (None, ""):
        return str(direct).strip()
    merchant = offer.get("merchant")
    if isinstance(merchant, dict):
        nested = (
            merchant.get("id")
            or merchant.get("uid")
            or merchant.get("merchantId")
            or merchant.get("merchantUID")
            or merchant.get("merchantUid")
        )
        if nested not in (None, ""):
            return str(nested).strip()
    return ""


def _identity(value: Any) -> str:
    return "".join(str(value or "").split()).casefold()


def _merchant_sku(offer: dict[str, Any]) -> str:
    value = (
        offer.get("merchantSku")
        or offer.get("merchantSKU")
        or offer.get("sku")
    )
    return str(value or "").strip()


def _own_match(
    offer: dict[str, Any],
    *,
    own_merchant_id: str,
    own_merchant_sku: str | None,
) -> str | None:
    merchant_id = _merchant_id(offer)
    normalized_merchant_id = _identity(merchant_id)
    normalized_own_id = _identity(own_merchant_id)
    if normalized_merchant_id and normalized_own_id == normalized_merchant_id:
        return "merchant_uid"
    # Some Offers API variants omit merchant identity but retain the exact
    # seller SKU. Use that fallback only when no conflicting merchant id is
    # present; a different explicit merchant can never become our own row.
    if (
        not merchant_id
        and own_merchant_sku
        and _identity(_merchant_sku(offer)) == _identity(own_merchant_sku)
    ):
        return "merchant_sku"
    return None


def _offer_debug(
    offer: dict[str, Any],
    own_merchant_id: str,
    *,
    own_merchant_sku: str | None,
    page_visible_price: Decimal | None,
    selected_competitor: dict[str, Any] | None = None,
    delivery_assessment: DeliveryAssessment | None = None,
    delivery_days: int | None = None,
) -> dict[str, Any]:
    price = _offer_price(offer)
    own_match = _own_match(
        offer,
        own_merchant_id=own_merchant_id,
        own_merchant_sku=own_merchant_sku,
    )
    is_own = own_match is not None
    decision_reason = (
        None if delivery_assessment is None else delivery_assessment.reason
    )
    used_for_dumping = not is_own and offer is selected_competitor
    if not is_own and page_visible_price is not None and price is not None and price < page_visible_price:
        decision_reason = "API price ниже цены, видимой на карточке; другой price/delivery context"
        used_for_dumping = False
    elif not is_own and delivery_assessment is not None and delivery_assessment.ignored:
        decision_reason = delivery_assessment.reason
        used_for_dumping = False
    elif (
        not is_own
        and selected_competitor is None
        and delivery_assessment is not None
        and delivery_assessment.price_gap_kzt is not None
        and delivery_assessment.price_gap_kzt <= 0
    ):
        decision_reason = (
            "Не выбран: наша текущая цена уже не выше этого оффера; "
            "повышение сверх защищённой доплаты за доставку запрещено."
        )
    elif not is_own and offer is not selected_competitor:
        decision_reason = "Не выбран: найден более выгодный допустимый ценовой ориентир."
    price_fields: dict[str, str] = {}
    for key, value in offer.items():
        if "price" in str(key).lower() and isinstance(value, (str, int, float)):
            price_fields[str(key)] = str(value)
    return {
        "merchant_id": _merchant_id(offer),
        "merchant_name": str(offer.get("merchantName") or "") or None,
        "is_own": is_own,
        "own_match": own_match,
        "price_kzt": None if price is None else format(price, "f"),
        "used_for_dumping": used_for_dumping,
        "ignored_reason": None if used_for_dumping else decision_reason,
        "decision_reason": decision_reason,
        "price_fields": price_fields,
        "delivery": _delivery_summary(offer),
        "delivery_days": (
            delivery_days
            if is_own
            else None if delivery_assessment is None else delivery_assessment.competitor_delivery_days
        ),
        "delivery_gap_days": (
            None if delivery_assessment is None else delivery_assessment.delivery_gap_days
        ),
        "price_gap_kzt": (
            None
            if delivery_assessment is None or delivery_assessment.price_gap_kzt is None
            else format(delivery_assessment.price_gap_kzt, "f")
        ),
    }


async def _request_with_retry(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
    response: httpx.Response | None = None
    for attempt in range(4):
        response = await client.request(method, url, **kwargs)
        if response.status_code != 429:
            response.raise_for_status()
            return response
        if attempt == 3:
            response.raise_for_status()
        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after else 1.5 * (2 ** attempt)
        except ValueError:
            delay = 1.5 * (2 ** attempt)
        await asyncio.sleep(min(8.0, max(0.8, delay)) + random.uniform(0.1, 0.5))
    assert response is not None
    response.raise_for_status()
    return response


async def _open_product_page(
    client: httpx.AsyncClient,
    *,
    master_id: str,
    city_id: str,
    product_name_hint: str | None,
) -> tuple[httpx.Response, dict[str, Any], str]:
    candidates: list[str] = []
    if product_name_hint:
        candidates.append(f"https://kaspi.kz/shop/p/{_slugify(product_name_hint)}-{master_id}/?c={city_id}")
    candidates.extend([
        f"https://kaspi.kz/shop/p/product-{master_id}/?c={city_id}",
        f"https://kaspi.kz/shop/p/{master_id}/?c={city_id}",
    ])
    seen: set[str] = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        try:
            page = await _request_with_retry(client, "GET", url, headers={
                "User-Agent": HEADERS["User-Agent"],
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": HEADERS["Accept-Language"],
            })
        except httpx.HTTPStatusError:
            continue
        promo = _promo_conditions(page.text)
        if promo is not None:
            return page, promo, str(page.url)
    raise ValueError(
        "Kaspi product page was not resolved from SKU. Set KASPI_PRODUCT_NAME in .env once for this test SKU and retry."
    )


async def scan_kaspi_competitors(
    *,
    kaspi_product_id: str,
    own_merchant_id: str,
    own_merchant_sku: str | None = None,
    city_id: str,
    zone_id: str,
    product_name_hint: str | None = None,
    product_brand_hint: str | None = None,
    max_pages: int = 20,
    delivery_price_premium_kzt: Decimal | int | str = 500,
    delivery_advantage_days: int = 5,
) -> KaspiCompetitorSnapshot:
    master_id = kaspi_product_id.split("_", 1)[0].strip()
    if not master_id:
        raise ValueError("SKU/master product id is empty")
    timeout = httpx.Timeout(25.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        page, promo, product_url = await _open_product_page(
            client,
            master_id=master_id,
            city_id=city_id,
            product_name_hint=product_name_hint,
        )
        product_name = product_name_hint or _page_title(page.text) or f"Kaspi product {master_id}"
        product_brand = str(promo.get("brand") or product_brand_hint or "").strip() or None
        page_visible_price = _page_visible_price(page.text)

        headers = dict(HEADERS)
        headers["Referer"] = str(page.url)
        headers["X-KS-City"] = city_id
        client.cookies.set("k_stat", str(uuid.uuid4()), domain="kaspi.kz", path="/")
        client.cookies.set("ks.tg", "27", domain="kaspi.kz", path="/")
        endpoint = f"https://kaspi.kz/yml/offer-view/offers/{quote(master_id)}"
        body_base = {
            "cityId": city_id,
            "id": master_id,
            "merchantUID": [],
            "limit": 5,
            "product": {
                "brand": promo.get("brand") or product_brand_hint or "",
                "categoryCodes": promo.get("categoryCodes") or [],
                "baseProductCodes": promo.get("baseProductCodes") or [],
                "groups": promo.get("groups"),
                "productSeries": promo.get("productSeries") or [],
            },
            "sortOption": "PRICE",
            "highRating": None,
            "searchText": None,
            "isExcellentMerchant": False,
            "zoneId": [zone_id],
            "installationId": "-1",
        }
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page_no in range(max_pages):
            body = dict(body_base)
            body["page"] = page_no
            response = await _request_with_retry(client, "POST", endpoint, headers=headers, json=body)
            page_rows = _offers(response.json())
            added = 0
            for offer in page_rows:
                identity = f"{_merchant_id(offer)}|{_merchant_sku(offer)}"
                if identity in seen:
                    continue
                seen.add(identity)
                rows.append(offer)
                added += 1
            if not page_rows or added == 0 or len(page_rows) < 5:
                break

    rows.sort(key=lambda row: (_offer_price(row) if _offer_price(row) is not None else Decimal("999999999"), str(row.get("merchantName") or "")))
    own_index = next(
        (
            index
            for index, row in enumerate(rows)
            if _own_match(
                row,
                own_merchant_id=own_merchant_id,
                own_merchant_sku=own_merchant_sku,
            )
            is not None
        ),
        None,
    )
    own = None if own_index is None else rows[own_index]

    external = [
        row
        for row in rows
        if _own_match(
            row,
            own_merchant_id=own_merchant_id,
            own_merchant_sku=own_merchant_sku,
        )
        is None
    ]
    market_context_ok = False
    market_context_reason: str | None = None
    trusted_external = external
    if page_visible_price is None:
        # API-only data is useful for diagnostics but not safe enough for live
        # autonomous repricing after observing a zone/context mismatch in Kaspi.
        market_context_reason = "Не удалось извлечь цену, видимую на публичной карточке; live-write заблокирован."
    else:
        trusted_external = [
            row for row in external
            if _offer_price(row) is not None and _offer_price(row) >= page_visible_price
        ]
        # The public headline is the lowest price visible to the buyer. If our
        # own offer is not that minimum, a trusted external offer must explain
        # that headline price exactly. Otherwise context remains ambiguous.
        own_price = None if own is None else _offer_price(own)
        exact_external = [row for row in trusted_external if _offer_price(row) == page_visible_price]
        if own_price == page_visible_price:
            market_context_ok = True
            market_context_reason = "Публичная цена совпадает с нашей; офферы ниже неё исключены как другой context."
        elif exact_external:
            market_context_ok = True
            market_context_reason = "Лучший конкурент подтверждён публичной ценой карточки."
        else:
            market_context_reason = (
                f"Публичная карточка показывает {page_visible_price} ₸, но API не дал внешний оффер с этой ценой; "
                "live-write заблокирован до совпадения контекста."
            )

    scan_date = _kaspi_today()
    competitor, delivery_assessments = _select_delivery_aware_competitor(
        own,
        trusted_external,
        max_price_premium_kzt=delivery_price_premium_kzt,
        min_delivery_advantage_days=delivery_advantage_days,
        today=scan_date,
    )
    delivery_filtered_count = sum(
        assessment.ignored for assessment in delivery_assessments.values()
    )
    delivery_selection_reason = None
    if delivery_filtered_count:
        if competitor is None:
            delivery_selection_reason = (
                f"Цена сохранена: {delivery_filtered_count} более дешёвых офферов "
                f"исключены, потому что наша доставка быстрее минимум на "
                f"{int(delivery_advantage_days)} дн. при разнице до "
                f"{format(Decimal(str(delivery_price_premium_kzt)), 'f')} ₸."
            )
        else:
            delivery_selection_reason = (
                f"Офферов исключено по преимуществу доставки: "
                f"{delivery_filtered_count}; выбран следующий допустимый конкурент."
            )
    own_delivery_days = _delivery_days(own, today=scan_date)
    diagnostics = tuple(
        _offer_debug(
            row,
            own_merchant_id,
            own_merchant_sku=own_merchant_sku,
            page_visible_price=page_visible_price,
            selected_competitor=competitor,
            delivery_assessment=delivery_assessments.get(id(row)),
            delivery_days=own_delivery_days,
        )
        for row in rows
    )
    return KaspiCompetitorSnapshot(
        product_name=product_name,
        product_brand=product_brand,
        own_price_kzt=None if own is None else _offer_price(own),
        competitor_price_kzt=None if competitor is None else _offer_price(competitor),
        competitor_name=None if competitor is None else str(competitor.get("merchantName") or "") or None,
        own_position=None if own_index is None else own_index + 1,
        seller_count=len(rows),
        product_url=product_url,
        own_delivery=_delivery_summary(own),
        competitor_delivery=_delivery_summary(competitor),
        offers=diagnostics,
        page_visible_price_kzt=page_visible_price,
        market_context_ok=market_context_ok,
        market_context_reason=market_context_reason,
        own_delivery_days=own_delivery_days,
        competitor_delivery_days=_delivery_days(competitor, today=scan_date),
        delivery_filtered_count=delivery_filtered_count,
        delivery_selection_reason=delivery_selection_reason,
    )
