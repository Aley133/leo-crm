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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://kaspi.kz",
    "Referer": "https://kaspi.kz/shop/",
}


@dataclass(frozen=True, slots=True)
class KaspiCompetitorSnapshot:
    own_price_kzt: Decimal | None
    competitor_price_kzt: Decimal | None
    competitor_name: str | None
    own_position: int | None
    seller_count: int
    product_url: str


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


def _merchant_id(offer: dict[str, Any]) -> str:
    return str(offer.get("merchantId") or offer.get("merchantUID") or "").strip()


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


async def scan_kaspi_competitors(
    *,
    product_name: str,
    product_brand: str | None,
    kaspi_product_id: str,
    own_merchant_id: str,
    city_id: str,
    zone_id: str,
    max_pages: int = 20,
) -> KaspiCompetitorSnapshot:
    master_id = kaspi_product_id.split("_", 1)[0].strip()
    product_url = f"https://kaspi.kz/shop/p/{_slugify(product_name)}-{master_id}/?c={city_id}"
    timeout = httpx.Timeout(25.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        page = await _request_with_retry(
            client,
            "GET",
            product_url,
            headers={
                "User-Agent": HEADERS["User-Agent"],
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": HEADERS["Accept-Language"],
            },
        )
        promo = _promo_conditions(page.text)
        if promo is None:
            raise ValueError("Kaspi promoConditions не найдены на карточке")

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
                "brand": promo.get("brand") or product_brand or "",
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
                identity = f"{_merchant_id(offer)}|{offer.get('merchantSku') or ''}"
                if identity in seen:
                    continue
                seen.add(identity)
                rows.append(offer)
                added += 1
            if not page_rows or added == 0 or len(page_rows) < 5:
                break

    rows.sort(key=lambda row: (Decimal(str(row.get("price") or "999999999")), str(row.get("merchantName") or "")))
    own_index = next((index for index, row in enumerate(rows) if _merchant_id(row) == own_merchant_id), None)
    own = None if own_index is None else rows[own_index]
    external = [row for row in rows if _merchant_id(row) != own_merchant_id]
    competitor = external[0] if external else None
    return KaspiCompetitorSnapshot(
        own_price_kzt=None if own is None or own.get("price") is None else Decimal(str(own["price"])),
        competitor_price_kzt=None if competitor is None or competitor.get("price") is None else Decimal(str(competitor["price"])),
        competitor_name=None if competitor is None else str(competitor.get("merchantName") or "") or None,
        own_position=None if own_index is None else own_index + 1,
        seller_count=len(rows),
        product_url=product_url,
    )
