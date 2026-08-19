from __future__ import annotations

import asyncio
import html
import random
import re
import time
import uuid
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx


class KaspiPhotoReadError(RuntimeError):
    pass


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Cache-Control": "no-cache",
    "Referer": "https://kaspi.kz/shop/",
}
_JSON_HEADERS = {
    "User-Agent": _BROWSER_HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": _BROWSER_HEADERS["Accept-Language"],
    "Referer": "https://kaspi.kz/shop/",
}
_MOBILE_PRODUCT_ENDPOINT = "https://kaspi.kz/shop/rest/misc/product/mobile"
_REQUEST_SPACING_SECONDS = 1.5
_REQUEST_GATE = asyncio.Lock()
_NEXT_REQUEST_AT = 0.0


def _slugify(value: str) -> str:
    table = str.maketrans({
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
        "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k",
        "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
        "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
        "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
        "э": "e", "ю": "yu", "я": "ya",
    })
    rendered = html.unescape(str(value or "")).lower().translate(table)
    return re.sub(r"[^0-9a-z]+", "-", rendered).strip("-") or "product"


def _with_city(value: str, city_id: str) -> str:
    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    query["c"] = [city_id]
    pairs = [(key, item) for key, values in query.items() for item in values]
    return urlunparse(parsed._replace(query=urlencode(pairs)))


def _product_id_from_url(value: str) -> str | None:
    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    for key in ("masterSku", "productCode"):
        candidate = str((query.get(key) or [""])[0]).strip()
        if re.fullmatch(r"\d{6,18}", candidate):
            return candidate
    match = re.search(r"-(\d{6,18})/?$", parsed.path.rstrip("/"))
    if match:
        return match.group(1)
    match = re.search(r"/(\d{6,18})/?$", parsed.path.rstrip("/"))
    return match.group(1) if match else None


def _og_image(page_html: str) -> str | None:
    patterns = (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, page_html, flags=re.I | re.S)
        if match:
            return html.unescape(match.group(1)).strip() or None
    return None


def _error_text(exc: Exception) -> str:
    detail = str(exc).strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def _mobile_gallery_image(payload: Any, *, expected_product_id: str) -> str:
    if not isinstance(payload, dict):
        raise KaspiPhotoReadError("JSON ответа Kaspi имеет неверный формат")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise KaspiPhotoReadError("в JSON ответа Kaspi отсутствует data")
    card = data.get("card")
    returned_product_id = str(card.get("id") if isinstance(card, dict) else "").strip()
    if returned_product_id != expected_product_id:
        raise KaspiPhotoReadError(
            f"Kaspi вернул другой товар: {returned_product_id or 'ID отсутствует'}"
        )
    gallery = data.get("galleryImages")
    if not isinstance(gallery, list) or not gallery:
        raise KaspiPhotoReadError("в JSON ответа Kaspi отсутствует galleryImages")
    first = gallery[0]
    if not isinstance(first, dict):
        raise KaspiPhotoReadError("первое изображение Kaspi имеет неверный формат")
    for size in ("large", "medium", "small"):
        candidate = str(first.get(size) or "").strip()
        if candidate:
            return candidate
    raise KaspiPhotoReadError("в galleryImages отсутствует URL фотографии")


async def _spaced_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    global _NEXT_REQUEST_AT
    async with _REQUEST_GATE:
        delay = _NEXT_REQUEST_AT - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            response = await client.get(url, headers=headers or _BROWSER_HEADERS)
        finally:
            _NEXT_REQUEST_AT = time.monotonic() + _REQUEST_SPACING_SECONDS
    return response


async def _request_card(client: httpx.AsyncClient, url: str) -> httpx.Response:
    response: httpx.Response | None = None
    for attempt in range(2):
        response = await _spaced_get(client, url)
        if response.status_code != 429:
            response.raise_for_status()
            return response
        if attempt == 1:
            response.raise_for_status()
        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after else 2.0
        except ValueError:
            delay = 2.0
        await asyncio.sleep(min(5.0, max(1.5, delay)) + random.uniform(0.1, 0.4))
    assert response is not None
    response.raise_for_status()
    return response


async def fetch_kaspi_product_photo(
    *,
    kaspi_product_id: str,
    product_name: str,
    city_id: str = "196220100",
) -> str:
    """Read a public Kaspi image URL with bounded backend-only HTTP traffic.

    Kaspi's compact product JSON is preferred because its public HTML card is
    rate-limited more aggressively. HTML/og:image remains a compatibility
    fallback. Requests are serialized because one CRM page can expose many
    missing-photo placeholders at once.
    """

    master_id = str(kaspi_product_id or "").split("_", 1)[0].strip()
    if not re.fullmatch(r"\d{6,18}", master_id):
        raise KaspiPhotoReadError("Kaspi product ID имеет неверный формат")

    candidates = [
        _with_city(
            f"https://kaspi.kz/shop/p/{_slugify(product_name)}-{master_id}/",
            city_id,
        ),
        _with_city(f"https://kaspi.kz/shop/p/product-{master_id}/", city_id),
    ]
    errors: list[str] = []
    timeout = httpx.Timeout(connect=7.0, read=12.0, write=7.0, pool=12.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        trust_env=False,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
    ) as client:
        client.cookies.set("k_stat", str(uuid.uuid4()), domain="kaspi.kz", path="/")
        client.cookies.set("ks.tg", "27", domain="kaspi.kz", path="/")
        mobile_url = f"{_MOBILE_PRODUCT_ENDPOINT}?{urlencode({'productCode': master_id, 'cityId': city_id})}"
        try:
            response = await _spaced_get(client, mobile_url, headers=_JSON_HEADERS)
            response.raise_for_status()
            return _mobile_gallery_image(response.json(), expected_product_id=master_id)
        except httpx.HTTPStatusError as exc:
            errors.append(f"JSON endpoint: {_error_text(exc)}")
            if exc.response.status_code == 429:
                detail = "; ".join(errors)
                raise KaspiPhotoReadError(f"JSON Kaspi временно ограничен ({detail})") from exc
        except (ValueError, httpx.HTTPError, KaspiPhotoReadError) as exc:
            errors.append(f"JSON endpoint: {_error_text(exc)}")

        for candidate in candidates:
            try:
                page = await _request_card(client, candidate)
                final_url = str(page.url)
                host = (urlparse(final_url).hostname or "").casefold()
                if host != "kaspi.kz" and not host.endswith(".kaspi.kz"):
                    raise KaspiPhotoReadError("Kaspi перенаправил запрос на внешний домен")
                resolved_id = _product_id_from_url(final_url) or _product_id_from_url(candidate)
                if resolved_id != master_id:
                    raise KaspiPhotoReadError(
                        f"Kaspi вернул другую карточку: {resolved_id or 'ID отсутствует'}"
                    )
                image_url = _og_image(page.text)
                if image_url:
                    return image_url
                raise KaspiPhotoReadError("в HTML карточки отсутствует og:image")
            except (httpx.HTTPError, KaspiPhotoReadError) as exc:
                errors.append(_error_text(exc))

    detail = "; ".join(errors[-2:]) or "неизвестная ошибка"
    raise KaspiPhotoReadError(f"фотография Kaspi не прочитана ({detail})")
