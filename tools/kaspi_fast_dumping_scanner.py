from __future__ import annotations

import asyncio
import html
import json
import random
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://kaspi.kz",
    "Referer": "https://kaspi.kz/shop/",
}


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
) -> dict[str, Any]:
    price = _offer_price(offer)
    own_match = _own_match(
        offer,
        own_merchant_id=own_merchant_id,
        own_merchant_sku=own_merchant_sku,
    )
    is_own = own_match is not None
    ignored_reason = None
    used_for_dumping = not is_own
    if not is_own and page_visible_price is not None and price is not None and price < page_visible_price:
        ignored_reason = "API price ниже цены, видимой на карточке; другой price/delivery context"
        used_for_dumping = False
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
        "ignored_reason": ignored_reason,
        "price_fields": price_fields,
        "delivery": _delivery_summary(offer),
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

    competitor = trusted_external[0] if trusted_external else None
    diagnostics = tuple(
        _offer_debug(
            row,
            own_merchant_id,
            own_merchant_sku=own_merchant_sku,
            page_visible_price=page_visible_price,
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
    )
