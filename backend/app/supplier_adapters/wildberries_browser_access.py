from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .base import AccessStrategy, AdapterRequest, NormalizedOffer
from .errors import AdapterBlockedError, AdapterCaptchaError, AdapterNetworkError, AdapterParseError, AdapterTimeoutError
from .playwright_pool import PlaywrightBrowserPool, PlaywrightPoolError

_WB_ROOT_DOMAINS = ("wildberries.ru", "wb.ru")
_WB_DESTINATIONS = ("123585444", "-1257786")
_WB_CARD_ENDPOINTS = (
    "https://card.wb.ru/cards/v2/detail",
    "https://card.wb.ru/cards/detail",
)
_PRICE_RE = re.compile(r"(\d[\d\s\u00a0\u202f]{1,12})\s*[₸₽]")
_DAY_RE = re.compile(r"(?:через\s*)?(\d{1,2})\s*(?:день|дня|дней|дн)\b", re.I)


class WildberriesBrowserAccessAdapter:
    """WB adapter with browser-verified price and API diagnostics.

    The adapter preserves the application boundary:
    AdapterRequest -> NormalizedOffer. It never writes Product, Binding,
    Observation, XML or queue state directly.
    """

    code = "wildberries-browser-verified-v5"
    access_strategy = AccessStrategy.BROWSER

    def __init__(
        self,
        pool: PlaywrightBrowserPool | None = None,
        *,
        timeout_seconds: float = 70.0,
        browser_fallback: bool | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._pool = pool or PlaywrightBrowserPool()
        self._timeout_seconds = timeout_seconds

    async def close(self) -> None:
        await self._pool.close()

    async def fetch(self, request: AdapterRequest) -> NormalizedOffer:
        self._validate_url(request.url)
        nm_id = self._extract_nm_id(request.url)
        if nm_id is None:
            raise AdapterParseError("Wildberries nmId was not found in supplier URL")

        api_offer, api_diagnostics = await self._load_api_diagnostics(request.url, nm_id)
        browser_offer, metadata = await self._fetch_browser_verified(request.url, nm_id)

        if api_offer is not None:
            for key in ("old_price", "stock", "delivery_days", "seller"):
                if browser_offer.get(key) is None:
                    browser_offer[key] = api_offer.get(key)

        return NormalizedOffer(
            supplier_product_id=request.supplier_product_id,
            price=browser_offer["price"],
            old_price=browser_offer.get("old_price"),
            currency=browser_offer.get("currency") or "KZT",
            available=browser_offer.get("available"),
            stock=browser_offer.get("stock"),
            delivery_days=browser_offer.get("delivery_days"),
            seller=browser_offer.get("seller"),
            adapter_schema_version="wildberries-browser-verified-v5",
            observed_at=datetime.now(UTC),
            raw_metadata={
                "source": "wb_browser_verified",
                "nm_id": nm_id,
                "api_diagnostics": api_diagnostics,
                **metadata,
            },
        )

    async def _fetch_browser_verified(
        self,
        url: str,
        nm_id: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        started = monotonic()
        timeout_ms = int(self._timeout_seconds * 1000)

        try:
            async with self._pool.isolated_page() as page:
                try:
                    await page.goto(url, wait_until="commit", timeout=min(timeout_ms, 25_000))
                except Exception as exc:
                    if exc.__class__.__name__ != "TimeoutError":
                        raise

                await page.wait_for_timeout(8_000)
                body_text = await self._body_text(page)
                normalized_text = body_text.casefold().replace("ё", "е")

                if self._is_antibot(normalized_text):
                    raise AdapterCaptchaError("Wildberries returned the suspicious activity screen")
                if any(marker in normalized_text for marker in ("access denied", "доступ ограничен", "forbidden")):
                    raise AdapterBlockedError("Wildberries blocked browser access")

                final_url = str(page.url)
                self._verify_product_page(final_url, body_text, nm_id)

                try:
                    await page.wait_for_function(
                        """() => /[₸₽]|добавить в корзину|купить сейчас|нет в наличии/i
                        .test(document.body?.innerText || '')""",
                        timeout=12_000,
                    )
                except Exception:
                    pass

                await page.mouse.wheel(0, 650)
                await page.wait_for_timeout(1_000)
                await page.mouse.wheel(0, -900)
                await page.wait_for_timeout(1_000)

                body_text = await self._body_text(page)
                stock_status, stock_reason = await self._detect_stock(page, body_text)
                if stock_status == "out_of_stock":
                    raise AdapterParseError("Wildberries product is out of stock")
                if stock_status != "in_stock":
                    raise AdapterParseError(
                        f"Wildberries stock could not be verified: {stock_reason}"
                    )

                price, candidates = await self._extract_visible_price(page)
                if price is None:
                    raise AdapterParseError(
                        "Wildberries card is available but visible purchase price was not found"
                    )

                seller = await self._seller_from_page(page)
                return (
                    {
                        "price": price,
                        "old_price": None,
                        "currency": "KZT",
                        "available": True,
                        "stock": None,
                        "delivery_days": self._delivery_days_from_text(body_text),
                        "seller": seller,
                    },
                    {
                        "response_url": final_url,
                        "duration_ms": int((monotonic() - started) * 1000),
                        "stock_reason": stock_reason,
                        "price_candidates": candidates[:10],
                    },
                )
        except (AdapterCaptchaError, AdapterBlockedError, AdapterParseError):
            raise
        except asyncio.TimeoutError as exc:
            raise AdapterTimeoutError("Wildberries browser verification timed out") from exc
        except PlaywrightPoolError:
            raise
        except Exception as exc:
            if exc.__class__.__name__ == "TimeoutError":
                raise AdapterTimeoutError("Wildberries browser verification timed out") from exc
            raise AdapterNetworkError(f"Wildberries browser verification failed: {exc}") from exc

    @staticmethod
    async def _body_text(page: Any) -> str:
        try:
            return str(await page.locator("body").inner_text(timeout=20_000))
        except Exception as exc:
            raise AdapterParseError(f"Wildberries page body is unavailable: {exc}") from exc

    @staticmethod
    def _is_antibot(text: str) -> bool:
        return any(
            marker in text
            for marker in (
                "подозрительная активность",
                "новая попытка через",
                "подтвердите, что вы не робот",
                "verify you are human",
                "captcha",
            )
        )

    @classmethod
    def _verify_product_page(cls, final_url: str, body_text: str, nm_id: int) -> None:
        final_id = cls._extract_nm_id(final_url)
        if final_id != nm_id:
            raise AdapterParseError(
                f"Wildberries opened another page: expected nmId={nm_id}, got {final_id}"
            )
        low = body_text.casefold().replace("ё", "е")
        for marker in (
            "страница не найдена",
            "товар не найден",
            "такой страницы нет",
            "ошибка 404",
        ):
            if marker in low:
                raise AdapterParseError(f"Wildberries product page is unavailable: {marker}")
        product_markers = (
            "артикул",
            "добавить в корзину",
            "купить сейчас",
            "характеристики",
            "описание",
        )
        if sum(marker in low for marker in product_markers) < 2:
            raise AdapterParseError("Wildberries page does not look like a product card")

    @staticmethod
    async def _detect_stock(page: Any, body_text: str) -> tuple[str, str]:
        low = body_text.casefold().replace("ё", "е")
        out_markers = (
            "нет в наличии",
            "товар закончился",
            "временно отсутствует",
            "распродано",
        )
        if any(marker in low for marker in out_markers):
            return "out_of_stock", "visible out-of-stock marker"

        selectors = (
            "button:has-text('Добавить в корзину')",
            "button:has-text('Купить сейчас')",
            "[data-testid*='cart']",
            "[class*='order'] button",
        )
        for selector in selectors:
            try:
                locator = page.locator(selector)
                for index in range(min(await locator.count(), 8)):
                    if await locator.nth(index).is_visible():
                        return "in_stock", f"visible purchase control: {selector}"
            except Exception:
                continue

        if "добавить в корзину" in low or "купить сейчас" in low:
            return "in_stock", "purchase text is visible"
        return "unknown", "purchase control and out-of-stock marker are both absent"

    @classmethod
    async def _extract_visible_price(
        cls,
        page: Any,
    ) -> tuple[Decimal | None, list[dict[str, Any]]]:
        selectors = (
            "ins.price-block__final-price",
            ".price-block__final-price",
            "[class*='price-block__final-price']",
            "[class*='final-price']",
            "[class*='finalPrice']",
            "[data-testid*='price']",
        )
        candidates: list[dict[str, Any]] = []

        for selector in selectors:
            try:
                locator = page.locator(selector)
                for index in range(min(await locator.count(), 20)):
                    element = locator.nth(index)
                    if not await element.is_visible():
                        continue
                    text = str(await element.inner_text(timeout=3_000)).strip()
                    price = cls._money_from_text(text)
                    if price is None:
                        continue
                    meta = await element.evaluate(
                        """el => {
                            const r = el.getBoundingClientRect();
                            const s = getComputedStyle(el);
                            return {
                                x: r.x, y: r.y,
                                fontSize: parseFloat(s.fontSize || '0'),
                                decoration: s.textDecorationLine || '',
                                className: String(el.className || '')
                            };
                        }"""
                    )
                    if "line-through" in str(meta.get("decoration", "")).casefold():
                        continue
                    candidates.append(
                        {
                            "price": str(price),
                            "text": text[:80],
                            "selector": selector,
                            "score": 1000 + float(meta.get("fontSize") or 0),
                            **meta,
                        }
                    )
            except Exception:
                continue

        try:
            generic = await page.locator("body").evaluate(
                """body => {
                    const result = [];
                    const currency = /(?:^|\\s)(\\d[\\d\\s\\u00a0\\u202f]{1,12})\\s*[₸₽](?:\\s|$)/;
                    for (const el of body.querySelectorAll('ins, span, div, p, strong, b')) {
                        const text = (el.innerText || '').replace(/[\\u00a0\\u202f]/g, ' ')
                            .replace(/\\s+/g, ' ').trim();
                        if (!text || text.length > 55 || !currency.test(text)) continue;
                        const style = getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        if (style.display === 'none' || style.visibility === 'hidden' ||
                            rect.width < 2 || rect.height < 2) continue;
                        const low = text.toLowerCase();
                        const decoration = String(style.textDecorationLine || '').toLowerCase();
                        const className = String(el.className || '').toLowerCase();
                        if (decoration.includes('line-through') || className.includes('old') ||
                            low.includes('месяц') || low.includes('рассроч')) continue;
                        let score = parseFloat(style.fontSize || '0');
                        if (className.includes('final')) score += 180;
                        if (className.includes('price')) score += 60;
                        if (el.tagName === 'INS') score += 100;
                        let ancestor = el;
                        for (let i = 0; i < 5 && ancestor; i++, ancestor = ancestor.parentElement) {
                            const t = (ancestor.innerText || '').toLowerCase();
                            if (t.includes('добавить в корзину') || t.includes('купить сейчас')) {
                                score += 220; break;
                            }
                        }
                        result.push({text, score, x: rect.x, y: rect.y, className});
                    }
                    return result.sort((a,b) => b.score-a.score).slice(0,30);
                }"""
            )
            for item in generic or []:
                price = cls._money_from_text(str(item.get("text") or ""))
                if price is not None:
                    candidates.append({"price": str(price), "selector": "DOM heuristic", **item})
        except Exception:
            pass

        if not candidates:
            return None, []
        candidates.sort(key=lambda item: -float(item.get("score") or 0))
        return Decimal(str(candidates[0]["price"])), candidates

    @staticmethod
    async def _seller_from_page(page: Any) -> str | None:
        selectors = (
            "[class*='seller'] a",
            "[data-testid*='seller']",
            "a[href*='/seller/']",
        )
        for selector in selectors:
            try:
                locator = page.locator(selector)
                for index in range(min(await locator.count(), 5)):
                    element = locator.nth(index)
                    if await element.is_visible():
                        text = " ".join(str(await element.inner_text(timeout=2_000)).split())
                        if 1 < len(text) < 120:
                            return text
            except Exception:
                continue
        return None

    @classmethod
    def _delivery_days_from_text(cls, body_text: str) -> int | None:
        lines = [" ".join(line.split()) for line in body_text.splitlines() if line.strip()]
        context_words = (
            "достав", "получ", "пункт", "пвз", "постамат",
            "курьер", "забрать", "самовывоз", "привез",
        )
        selected: list[str] = []
        for index, line in enumerate(lines):
            low = line.casefold().replace("ё", "е")
            if any(word in low for word in context_words):
                selected.extend(lines[max(0, index - 2): index + 5])
        text = " ".join(selected).casefold().replace("ё", "е")
        if not text:
            return None
        if "сегодня" in text:
            return 0
        if "послезавтра" in text:
            return 2
        if "завтра" in text:
            return 1
        values = [int(match) for match in _DAY_RE.findall(text)]
        return min(values) if values else None

    async def _load_api_diagnostics(
        self,
        referer_url: str,
        nm_id: int,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        errors: list[str] = []
        for destination in _WB_DESTINATIONS:
            for endpoint in _WB_CARD_ENDPOINTS:
                try:
                    payload = await asyncio.to_thread(
                        self._fetch_card_payload,
                        endpoint,
                        nm_id,
                        destination,
                        referer_url,
                    )
                    product = self._find_product(payload, nm_id)
                    if product is None:
                        errors.append(f"{endpoint} dest={destination}: product not found")
                        continue
                    offer = self._offer_from_node(product)
                    if offer is None:
                        errors.append(f"{endpoint} dest={destination}: price unavailable")
                        continue
                    return offer, errors
                except Exception as exc:
                    errors.append(f"{endpoint} dest={destination}: {type(exc).__name__}: {exc}")
        return None, errors

    @staticmethod
    def _headers(referer_url: str) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Origin": "https://www.wildberries.ru",
            "Referer": referer_url,
        }

    def _fetch_card_payload(
        self,
        endpoint: str,
        nm_id: int,
        destination: str,
        referer_url: str,
    ) -> dict[str, Any]:
        query = urlencode(
            {
                "appType": "1",
                "curr": "kzt",
                "dest": destination,
                "spp": "30",
                "ab_testing": "false",
                "lang": "ru",
                "nm": str(nm_id),
            }
        )
        return self._get_json(f"{endpoint}?{query}", headers=self._headers(referer_url))

    def _get_json(self, url: str, *, headers: dict[str, str]) -> dict[str, Any]:
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=min(self._timeout_seconds, 12.0)) as response:
                if response.status != 200:
                    raise AdapterNetworkError(f"WB API returned HTTP {response.status}")
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise AdapterNetworkError(f"WB API returned HTTP {exc.code}") from exc
        except URLError as exc:
            raise AdapterNetworkError(f"WB API unavailable: {exc.reason}") from exc
        except TimeoutError as exc:
            raise AdapterTimeoutError("WB API request timed out") from exc
        except json.JSONDecodeError as exc:
            raise AdapterParseError("WB API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise AdapterParseError("WB API returned an unexpected payload")
        return payload

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        supported = any(host == root or host.endswith(f".{root}") for root in _WB_ROOT_DOMAINS)
        if parsed.scheme not in {"http", "https"} or not supported:
            raise ValueError("Wildberries adapter accepts only wildberries.ru or wb.ru URLs")

    @staticmethod
    def _extract_nm_id(url: str) -> int | None:
        parsed = urlparse(url)
        candidates = re.findall(
            r"(?:catalog/|product/|nm=)(\d{5,})",
            f"{parsed.path}?{parsed.query}",
        )
        if not candidates:
            candidates = re.findall(r"\b(\d{5,})\b", parsed.path)
        return int(candidates[0]) if candidates else None

    @classmethod
    def _find_product(cls, payload: Any, nm_id: int) -> dict[str, Any] | None:
        fallback: dict[str, Any] | None = None
        for node in cls._walk_dicts(payload):
            if not cls._looks_like_product(node):
                continue
            fallback = fallback or node
            raw_id = node.get("id", node.get("nmId", node.get("nmID", node.get("nm_id"))))
            try:
                if raw_id is not None and int(raw_id) == nm_id:
                    return node
            except (TypeError, ValueError):
                continue
        return fallback

    @classmethod
    def _looks_like_product(cls, node: dict[str, Any]) -> bool:
        keys = set(node)
        if not keys & {"id", "nmId", "nmID", "nm_id", "name", "brand"}:
            return False
        if keys & {"salePriceU", "priceU", "salePrice", "price", "finalPrice", "clientPrice"}:
            return True
        sizes = node.get("sizes")
        return isinstance(sizes, list) and any(
            isinstance(size, dict)
            and (
                isinstance(size.get("price"), dict)
                or any(key in size for key in ("salePriceU", "priceU", "price"))
            )
            for size in sizes
        )

    @classmethod
    def _offer_from_node(cls, node: dict[str, Any]) -> dict[str, Any] | None:
        price = cls._extract_price(node)
        if price is None:
            return None
        old_price = cls._extract_old_price(node)
        stock = cls._stock_from_node(node)
        available = cls._availability(
            node.get("available", node.get("isAvailable", node.get("availability")))
        )
        if available is None and stock is not None:
            available = stock > 0
        seller = (
            node.get("supplierName")
            or node.get("sellerName")
            or node.get("seller")
            or node.get("supplier")
        )
        if isinstance(seller, dict):
            seller = seller.get("name")
        return {
            "price": price,
            "old_price": old_price if old_price and old_price != price else None,
            "currency": str(node.get("currencyCode") or node.get("currency") or "KZT").upper(),
            "available": available,
            "stock": stock,
            "delivery_days": cls._delivery_days(node),
            "seller": str(seller).strip() if seller not in (None, "") else None,
        }

    @classmethod
    def _extract_price(cls, node: dict[str, Any]) -> Decimal | None:
        for key in (
            "salePriceU", "priceU", "salePrice", "price",
            "sale_price", "finalPrice", "clientPrice",
        ):
            value = node.get(key)
            if isinstance(value, dict):
                continue
            price = cls._normalize_wb_price(value, minimal_units=key.endswith("U"))
            if price is not None:
                return price
        sizes = node.get("sizes")
        if isinstance(sizes, list):
            for size in sizes:
                if not isinstance(size, dict):
                    continue
                price_info = size.get("price")
                if isinstance(price_info, dict):
                    for key in ("total", "product", "basic", "final", "discounted", "price"):
                        price = cls._normalize_wb_price(price_info.get(key), minimal_units=True)
                        if price is not None:
                            return price
        return None

    @classmethod
    def _extract_old_price(cls, node: dict[str, Any]) -> Decimal | None:
        for key in ("priceU", "oldPrice", "basicPrice", "originalPrice"):
            price = cls._normalize_wb_price(
                node.get(key),
                minimal_units=key.endswith("U"),
            )
            if price is not None:
                return price
        sizes = node.get("sizes")
        if isinstance(sizes, list):
            for size in sizes:
                if isinstance(size, dict) and isinstance(size.get("price"), dict):
                    price = cls._normalize_wb_price(size["price"].get("basic"), minimal_units=True)
                    if price is not None:
                        return price
        return None

    @classmethod
    def _stock_from_node(cls, node: dict[str, Any]) -> int | None:
        for key in ("totalQuantity", "quantity", "stock", "totalStock"):
            value = cls._integer(node.get(key))
            if value is not None:
                return value
        sizes = node.get("sizes")
        if not isinstance(sizes, list):
            return None
        total = 0
        found = False
        for size in sizes:
            if not isinstance(size, dict):
                continue
            stocks = size.get("stocks")
            if not isinstance(stocks, list):
                continue
            for stock in stocks:
                if not isinstance(stock, dict):
                    continue
                qty = cls._integer(stock.get("qty", stock.get("quantity")))
                if qty is not None:
                    total += qty
                    found = True
        return total if found else None

    @classmethod
    def _delivery_days(cls, node: dict[str, Any]) -> int | None:
        values: list[int] = []
        for key in ("deliveryTime", "delivery_time", "time1", "time2", "time"):
            value = cls._integer(node.get(key))
            if value is not None:
                values.append(value)
        nested = node.get("logistics") or node.get("delivery")
        if isinstance(nested, dict):
            for key in ("deliveryTime", "delivery_time", "time1", "time2", "time", "days"):
                value = cls._integer(nested.get(key))
                if value is not None:
                    values.append(value)
        values = [value for value in values if 0 <= value <= 720]
        if not values:
            return None
        value = min(values)
        return max(0, min(30, (value + 23) // 24 if value > 30 else value))

    @staticmethod
    def _normalize_wb_price(value: Any, *, minimal_units: bool) -> Decimal | None:
        if value is None or isinstance(value, bool):
            return None
        normalized = re.sub(r"[^0-9,.-]", "", str(value)).replace(",", ".")
        if not normalized:
            return None
        try:
            result = Decimal(normalized)
        except (InvalidOperation, TypeError, ValueError):
            return None
        if minimal_units or result >= Decimal("100000"):
            result /= Decimal("100")
        return result if result >= Decimal("100") else None

    @staticmethod
    def _money_from_text(value: str) -> Decimal | None:
        match = _PRICE_RE.search(value.replace("\xa0", " ").replace("\u202f", " "))
        if not match:
            return None
        normalized = re.sub(r"\s+", "", match.group(1))
        try:
            result = Decimal(normalized)
        except InvalidOperation:
            return None
        return result if Decimal("100") <= result <= Decimal("10000000") else None

    @classmethod
    def _walk_dicts(cls, value: Any) -> Iterable[dict[str, Any]]:
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from cls._walk_dicts(child)
        elif isinstance(value, list):
            for child in value:
                yield from cls._walk_dicts(child)

    @staticmethod
    def _integer(value: Any) -> int | None:
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _availability(value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        text = str(value or "").casefold()
        if any(marker in text for marker in ("instock", "in_stock", "available", "true")):
            return True
        if any(marker in text for marker in ("outofstock", "out_of_stock", "soldout", "false")):
            return False
        return None
