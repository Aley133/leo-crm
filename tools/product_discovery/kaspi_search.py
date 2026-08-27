from __future__ import annotations

import math
import secrets
import time
from typing import Any
from urllib.parse import quote

import httpx

BASE = "https://kaspi.kz"
FILTERS_URL = f"{BASE}/yml/product-view/pl/filters"
RESULTS_URL = f"{BASE}/yml/product-view/pl/results"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
VALID_SORTS = {"relevance", "price-asc", "price-desc", "rating", "created-desc"}
VALID_MODES = {"text", "brand"}
MAX_BATCH = 1000
ASSUMED_PAGE_SIZE = 12
# Exact q observed in Chrome Network while paging through Solgar results.
NETWORK_Q = ":availableInZones:Magnum_ZONE1:category:Categories"
REFERER_Q = ":availableInZones:Magnum_ZONE1"


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip().replace("\xa0", " ")
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


def _brand(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("name", "title", "value", "code"):
            candidate = str(value.get(key) or "").strip()
            if candidate:
                return candidate
    return None


def _images(card: dict[str, Any]) -> list[str]:
    images = card.get("previewImages")
    if not isinstance(images, list):
        return []
    out: list[str] = []
    for row in images:
        if not isinstance(row, dict):
            continue
        value = row.get("large") or row.get("medium") or row.get("small")
        if isinstance(value, str) and value.startswith("http") and value not in out:
            out.append(value)
    return out


def _image(card: dict[str, Any]) -> str | None:
    images = _images(card)
    return images[0] if images else None


def _link(raw: Any, city_id: str) -> str | None:
    value = str(raw or "").strip()
    if not value:
        return None
    if value.startswith("http"):
        url = value
    elif value.startswith("/p/"):
        url = BASE + "/shop" + value
    else:
        url = BASE + value
    # shopLink in /results already normally contains c=. Do not duplicate it.
    if "?c=" in url or "&c=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}c={city_id}"


def normalize_card(card: dict[str, Any], city_id: str) -> dict[str, Any]:
    category = card.get("category")
    if isinstance(category, list):
        category_text = ", ".join(str(x) for x in category if x)
    elif isinstance(category, dict):
        category_text = str(category.get("title") or category.get("name") or category.get("code") or "").strip() or None
    else:
        category_text = str(category or "").strip() or None
    return {
        "master_sku": str(card.get("id") or card.get("configSku") or "").strip(),
        "title": str(card.get("title") or "").strip() or None,
        "brand": _brand(card.get("brand")),
        "price_kzt": card.get("unitSalePrice") or card.get("unitPrice"),
        "price_formatted": card.get("priceFormatted"),
        "rating": card.get("rating"),
        "reviews": card.get("reviewsQuantity"),
        "delivery": card.get("deliveryDuration"),
        "category": category_text,
        "best_merchant": card.get("bestMerchant"),
        "image_url": _image(card),
        "image_urls": _images(card),
        "kaspi_url": _link(card.get("shopLink"), city_id),
        "created_time": card.get("createdTime"),
        "stock": card.get("stock"),
    }


def _extract_cards(payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse both Kaspi shapes seen in discovery.

    /filters -> {"data": {"cards": [...], ...}}
    /results -> {"data": [...], "promotedCards": ...}
    """
    if not isinstance(payload, dict):
        raise RuntimeError("Kaspi storefront вернул не JSON-объект")
    data = payload.get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)], {}
    if isinstance(data, dict):
        cards = data.get("cards")
        if isinstance(cards, list):
            return [x for x in cards if isinstance(x, dict)], data
    raise RuntimeError("Kaspi storefront вернул JSON без списка карточек")


class KaspiProductSearch:
    """Pure HTTP JSON storefront discovery. No write endpoints exist here."""

    def __init__(self, city_id: str, timeout: float = 15.0, page_delay_ms: int = 70) -> None:
        self.city_id = str(city_id or "750000000")
        self.page_delay_ms = max(0, int(page_delay_ms))
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "Accept": "application/json, text/*",
                "User-Agent": UA,
                "Accept-Language": "ru,en-US;q=0.9,en;q=0.8,kk;q=0.7",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "X-KS-City": self.city_id,
                "Cookie": f"kaspi.storefront.cookie.city={self.city_id}",
            },
        )

    def close(self) -> None:
        self.client.close()

    def _request_id(self) -> str:
        # Chrome HAR showed one stable 32-hex requestId reused while paging.
        return secrets.token_hex(16)

    def _results_params(self, text: str, page: int, request_id: str) -> dict[str, Any]:
        return {
            "page": page,
            "q": NETWORK_Q,
            "text": text,
            "sort": "relevance",
            "qs": "",
            "requestId": request_id,
            "ui": "d",
            "i": "-1",
            "c": self.city_id,
        }

    def _filters_params(self, text: str, page: int = 0) -> dict[str, Any]:
        # Bootstrap fallback only. Deep paging must use /results, per HAR.
        return {
            "page": page,
            "q": NETWORK_Q,
            "text": text,
            "sort": "relevance",
            "ui": "d",
            "i": "-1",
            "c": self.city_id,
        }

    def _referer(self, text: str, page: int) -> str:
        base = (
            f"{BASE}/shop/search/?text={quote(text)}"
            f"&q={quote(REFERER_Q, safe='')}"
            "&sort=relevance&filteredByCategory=false&sc="
        )
        return base if page <= 1 else f"{base}&page={page}"

    def _get_page(self, text: str, page: int, request_id: str) -> tuple[httpx.Response, str, dict[str, Any]]:
        params = self._results_params(text, page, request_id)
        headers = {"Referer": self._referer(text, page)}
        response = self.client.get(RESULTS_URL, params=params, headers=headers)
        return response, RESULTS_URL, params

    def _bootstrap_page_zero(self, text: str, request_id: str) -> tuple[httpx.Response, str, dict[str, Any]]:
        # Prefer the exact /results transport even for page=0. If Kaspi rejects
        # it, use /filters only for the first page; pages 1+ still use /results.
        response, url, params = self._get_page(text, 0, request_id)
        if response.status_code != 400:
            return response, url, params
        params = self._filters_params(text, 0)
        headers = {"Referer": self._referer(text, 0)}
        response = self.client.get(FILTERS_URL, params=params, headers=headers)
        return response, FILTERS_URL, params

    def _scan_once(self, text: str, *, page: int, sort: str, limit: int, mode: str) -> dict[str, Any]:
        products: list[dict[str, Any]] = []
        seen: set[str] = set()
        seen_page_signatures: set[tuple[str, ...]] = set()
        current = page
        first_meta: dict[str, Any] = {}
        requests: list[dict[str, Any]] = []
        duplicates = 0
        started_total = time.perf_counter()
        request_id = self._request_id()

        max_pages = min(120, max(1, math.ceil(limit / ASSUMED_PAGE_SIZE) + 16))
        pages_attempted = 0
        successful_pages = 0
        stop_reason = "limit_reached"

        while len(products) < limit and pages_attempted < max_pages:
            started = time.perf_counter()
            if current == 0:
                response, url, params = self._bootstrap_page_zero(text, request_id)
            else:
                response, url, params = self._get_page(text, current, request_id)
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            request_row = {
                "page": current,
                "visual_page": current + 1,
                "mode": mode,
                "method": "GET",
                "url": url,
                "params": params,
                "status_code": response.status_code,
                "elapsed_ms": elapsed,
            }
            requests.append(request_row)
            pages_attempted += 1

            if response.status_code == 429:
                raise RuntimeError(
                    f"Kaspi storefront вернул HTTP 429 на внутренней page={current}. "
                    "Остановили пачку, чтобы не усиливать лимит."
                )
            if response.status_code == 400:
                request_row["response_preview"] = response.text[:500]
                # Once /results has already returned valid pages, a 400 on the
                # next page is treated as the end boundary of this result set.
                if successful_pages > 0:
                    stop_reason = "results_http_400_after_success"
                    break
                raise httpx.HTTPStatusError(
                    f"Kaspi storefront HTTP 400 on page={current}",
                    request=response.request,
                    response=response,
                )
            response.raise_for_status()
            successful_pages += 1
            payload = response.json()
            cards, meta = _extract_cards(payload)
            if not first_meta and meta:
                first_meta = meta
            if not cards:
                stop_reason = "empty_page"
                break

            page_skus: list[str] = []
            page_new = 0
            for card in cards:
                normalized = normalize_card(card, self.city_id)
                master_sku = normalized["master_sku"]
                if not master_sku:
                    continue
                page_skus.append(master_sku)
                if master_sku in seen:
                    duplicates += 1
                    continue
                seen.add(master_sku)
                products.append(normalized)
                page_new += 1
                if len(products) >= limit:
                    break

            signature = tuple(page_skus)
            if signature and signature in seen_page_signatures:
                stop_reason = "repeated_page"
                break
            if signature:
                seen_page_signatures.add(signature)
            if page_new == 0:
                stop_reason = "no_new_cards"
                break
            if len(cards) < ASSUMED_PAGE_SIZE and len(products) < limit:
                stop_reason = "short_last_page"
                break

            current += 1
            if self.page_delay_ms and len(products) < limit:
                time.sleep(self.page_delay_ms / 1000)
        else:
            if pages_attempted >= max_pages and len(products) < limit:
                stop_reason = "page_safety_limit"

        # Deep transport is known-good in relevance order from Network HAR.
        # Other UI sorts are applied locally after collecting cards.
        if sort == "created-desc":
            products.sort(key=lambda x: str(x.get("created_time") or ""), reverse=True)
        elif sort == "price-asc":
            products.sort(key=lambda x: (x.get("price_kzt") is None, x.get("price_kzt") or 0))
        elif sort == "price-desc":
            products.sort(key=lambda x: (x.get("price_kzt") is not None, x.get("price_kzt") or 0), reverse=True)
        elif sort == "rating":
            products.sort(key=lambda x: (x.get("rating") is not None, x.get("rating") or 0), reverse=True)

        return {
            "query": text,
            "mode": mode,
            "sort": sort,
            "page": page,
            "request_id": request_id,
            "transport_sort": "relevance",
            "total": first_meta.get("total"),
            "category_title": first_meta.get("title"),
            "products": products[:limit],
            "http": requests,
            "stats": {
                "requested_limit": limit,
                "available_matches": _as_int(first_meta.get("total")),
                "unique_cards": len(products[:limit]),
                "duplicates_skipped": duplicates,
                "pages_requested": len(requests),
                "successful_pages": successful_pages,
                "first_page": page,
                "last_page": requests[-1]["page"] if requests else page,
                "elapsed_ms": round((time.perf_counter() - started_total) * 1000, 1),
                "stop_reason": stop_reason,
                "transport": "network_har_results_flow",
            },
        }

    def search(
        self,
        text: str,
        *,
        page: int = 0,
        sort: str = "created-desc",
        limit: int = 100,
        mode: str = "brand",
        allow_sort_fallback: bool = True,
    ) -> dict[str, Any]:
        del allow_sort_fallback  # transport is always relevance; sort is local
        text = str(text or "").strip()
        if not text:
            raise ValueError("Введи название, бренд или поисковую фразу")
        sort = sort if sort in VALID_SORTS else "created-desc"
        mode = mode if mode in VALID_MODES else "brand"
        page = max(0, int(page))
        limit = min(MAX_BATCH, max(1, int(limit)))

        result = self._scan_once(text, page=page, sort=sort, limit=limit, mode=mode)
        result["requested_sort"] = sort
        result["effective_sort"] = sort
        result["sort_fallback"] = sort != "relevance"
        result["sort_fallback_reason"] = (
            "Kaspi deep pagination uses relevance in the captured Network flow; "
            "requested sort is applied locally after HTTP collection."
            if sort != "relevance" else None
        )
        return result
