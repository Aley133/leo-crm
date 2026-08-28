from __future__ import annotations

import json
import time
from typing import Any

from .config import Config, ROOT
from .parser import parse_search, cheaper_price_hint, parse_other_seller_offers, parse_product_page
from .session_profile import CurlProfile


class OzonSessionHttpClient:
    """Replay an authenticated/accepted browser HTTP request without launching a browser.

    The copied cURL (including cookies) is kept only in process memory by ui_server.py.
    """

    def __init__(self, profile: CurlProfile, config: Config | None = None):
        self.profile = profile
        self.config = config or Config.load()
        try:
            from curl_cffi import requests as curl_requests
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("Не установлен curl_cffi. Запусти RUN_UI.cmd ещё раз.") from exc
        self._requests = curl_requests
        self.session = curl_requests.Session(impersonate=self.config.impersonate)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    @staticmethod
    def _blocked(resp, text_head: str) -> bool:
        low = text_head.lower()
        return resp.status_code in {401, 403, 429} or "captcha" in low or "incidentid" in low or "supporturl" in low

    def _do(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        resp = self.session.get(
            url,
            headers=headers or self.profile.request_headers(),
            timeout=self.config.timeout,
            allow_redirects=True,
        )
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        text = resp.text or ""
        head = text[:600]
        cookie_updates: dict[str, str] = {}
        for jar in (getattr(resp, "cookies", None), getattr(self.session, "cookies", None)):
            if jar is None:
                continue
            try:
                values = jar.get_dict()
            except Exception:
                values = {}
            if isinstance(values, dict):
                for key, value in values.items():
                    cookie_updates[str(key)] = str(value)
        changed_cookies = self.profile.merge_cookie_values(cookie_updates)

        result: dict[str, Any] = {
            "status_code": resp.status_code,
            "elapsed_ms": elapsed,
            "content_type": (resp.headers.get("content-type") or "").lower(),
            "bytes": len(resp.content or b""),
            "final_url": str(resp.url),
            "blocked": self._blocked(resp, head),
            "cookie_updates_seen": len(cookie_updates),
            "cookie_values_changed": changed_cookies,
        }
        if resp.status_code != 200:
            result["body_preview"] = head.replace("\n", " ")[:300]
            return result
        try:
            payload = resp.json()
        except Exception:
            result["json"] = False
            result["body_preview"] = head.replace("\n", " ")[:300]
            return result
        result["json"] = isinstance(payload, dict)
        if not isinstance(payload, dict):
            result["payload_type"] = type(payload).__name__
            return result

        parsed = parse_search(payload, base=self.profile.origin, max_results=self.config.max_results, expected_currency=self.config.expected_currency)
        result["parsed_items"] = len(parsed["items"])
        result["widget_keys"] = parsed["widget_keys"][:20]
        result["payload"] = payload
        result["parsed"] = parsed
        return result

    @staticmethod
    def _product_id(product_url: str, explicit_product_id: str | int | None = None) -> str:
        if explicit_product_id is not None and str(explicit_product_id).strip().isdigit():
            return str(explicit_product_id).strip()
        from urllib.parse import urlsplit
        import re
        path = urlsplit(str(product_url or "").strip()).path
        match = re.search(r"-(\d{6,})(?:/)?$", path.rstrip("/") + "/")
        if not match:
            nums = re.findall(r"\d{6,}", path)
            if nums:
                return nums[-1]
            raise ValueError("Не удалось определить Ozon product_id из URL")
        return match.group(1)

    def other_seller_offers(self, product_url: str, product_id: str | int | None = None) -> dict[str, Any]:
        """Load Ozon's exact 'Другие предложения от продавцов' modal by HTTP.

        Captured from the real storefront Network flow:
        `/modal/otherOffersFromSellers?product_id=<id>&sort=price&page_changed=true`.
        The modal response contains seller-specific SKU, seller, price and delivery.
        """
        from urllib.parse import urlencode

        pid = self._product_id(product_url, product_id)
        inner = "/modal/otherOffersFromSellers?" + urlencode({
            "product_id": pid,
            "sort": "price",
            "page_changed": "true",
        })
        url = self.profile.rewritten_inner_url(inner)
        headers = self.profile.request_headers_for_page(product_url)
        # Match the browser modal request more closely without persisting any
        # additional credentials. Dynamic IDs from the imported cURL are safe to
        # reuse; x-page-previous is empty in the captured modal flow.
        for key in list(headers.keys()):
            if key.lower() == "x-page-previous":
                headers[key] = ""
        raw = self._do(url, headers=headers)
        payload = raw.pop("payload", None)
        raw.pop("parsed", None)
        parsed = parse_other_seller_offers(payload or {}, base=self.profile.origin, expected_currency=self.config.expected_currency) if isinstance(payload, dict) else {"offers": [], "cheapest": None}
        cheapest = parsed.get("cheapest") if isinstance(parsed, dict) else None
        return {
            "ok": raw.get("status_code") == 200 and not raw.get("blocked"),
            "attempt": raw,
            "product_id": pid,
            "offer_count": int((parsed or {}).get("offer_count") or 0),
            "offers": list((parsed or {}).get("offers") or []),
            "cheapest_offer": cheapest,
            "read_only": True,
        }

    def product_price_hints(self, product_url: str, product_id: str | int | None = None) -> dict[str, Any]:
        """Return the cheapest seller offer from Ozon's exact other-offers modal.

        Kept under the historical method name so the pipeline API remains stable.
        """
        result = self.other_seller_offers(product_url, product_id=product_id)
        cheapest = result.get("cheapest_offer") or {}
        value = cheapest.get("price_kzt")
        return {
            "ok": bool(result.get("ok")),
            "attempt": result.get("attempt") or {},
            "cheaper_price_kzt": value if isinstance(value, int) and value > 0 else None,
            "cheaper_price_text": cheapest.get("price_text"),
            "cheaper_price_source": "otherOffersFromSellers.webSellerList" if cheapest else None,
            "cheaper_offer": cheapest or None,
            "other_offer_count": result.get("offer_count") or 0,
            "other_offers": result.get("offers") or [],
            "product_id": result.get("product_id"),
            "read_only": True,
        }

    def product_page_price(self, product_url: str, product_id: str | int | None = None) -> dict[str, Any]:
        """Read the exact card's displayed KZT price through the saved HTTP session."""

        pid = self._product_id(product_url, product_id)
        raw = self._do(
            self.profile.rewritten_page_url(product_url),
            headers=self.profile.request_headers_for_page(product_url),
        )
        payload = raw.pop("payload", None)
        raw.pop("parsed", None)
        card = (
            parse_product_page(
                payload,
                base=self.profile.origin,
                expected_currency=self.config.expected_currency,
            )
            if isinstance(payload, dict)
            else {}
        )
        price = card.get("price_kzt")
        return {
            "ok": bool(
                raw.get("status_code") == 200
                and not raw.get("blocked")
                and isinstance(price, int)
                and not isinstance(price, bool)
                and price > 0
            ),
            "attempt": raw,
            "product_id": pid,
            "card": card,
            "price_kzt": price if isinstance(price, int) and not isinstance(price, bool) and price > 0 else None,
            "price_text": card.get("price_text"),
            "price_source": card.get("price_source"),
            "delivery_text": card.get("delivery_text"),
            "delivery_date": card.get("delivery_date"),
            "delivery_days": card.get("delivery_days"),
            "read_only": True,
        }

    def replay_exact(self) -> dict[str, Any]:
        raw = self._do(self.profile.url)
        return self._finish(raw, mode="exact_replay", query=None, page=None)

    def search(self, query: str, page: int = 1) -> dict[str, Any]:
        url = self.profile.rewritten_search_url(query, page)
        raw = self._do(url, headers=self.profile.request_headers_for_search(query, page))
        return self._finish(raw, mode="session_search", query=query, page=page)

    def _finish(self, raw: dict[str, Any], mode: str, query: str | None, page: int | None) -> dict[str, Any]:
        payload = raw.pop("payload", None)
        parsed = raw.pop("parsed", None)
        if isinstance(payload, dict):
            data_dir = ROOT / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "last_ozon_response.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        items = list((parsed or {}).get("items") or [])
        ok = raw.get("status_code") == 200 and bool(items)
        reason = None
        if not ok:
            if raw.get("blocked"):
                reason = "blocked"
            elif raw.get("status_code") == 200 and raw.get("json") and not items:
                reason = "parser_drift_or_no_items"
            else:
                reason = "transport_failed"
        return {
            "ok": ok,
            "mode": mode,
            "reason": reason,
            "query": query,
            "page": page,
            "attempt": raw,
            "items": items,
            "parser": (parsed or {}).get("parser"),
            "widget_keys": (parsed or {}).get("widget_keys") or [],
            "response_saved": "data/last_ozon_response.json" if isinstance(payload, dict) else None,
            "read_only": True,
            "playwright_used": False,
            "browser_used": False,
        }
