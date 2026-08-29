from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

import httpx


BASE = "https://kaspi.kz/shop/api/products"


class OfficialProductsApi:
    """Minimal official Kaspi Product Import client used only by Product Test."""

    def __init__(self, api_token: str, *, timeout: float = 30.0) -> None:
        token = str(api_token or "").strip()
        if not token:
            raise ValueError("Kaspi API-токен для новых карточек не настроен")
        self.client = httpx.Client(
            headers={
                "Accept": "application/json",
                "X-Auth-Token": token,
                "User-Agent": "LEO-Product-Test-Agent/new-card",
            },
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self.client.close()

    def _get(self, url: str) -> Any:
        response = self.client.get(url)
        response.raise_for_status()
        return response.json()

    def categories(self) -> Any:
        return self._get(f"{BASE}/classification/categories")

    def attributes(self, category: str) -> Any:
        return self._get(f"{BASE}/classification/attributes?c={quote(category)}")

    def attribute_values(self, category: str, attribute: str) -> Any:
        return self._get(
            f"{BASE}/classification/attribute/values?c={quote(category)}&a={quote(attribute)}"
        )

    def import_status(self, code: str) -> Any:
        return self._get(f"{BASE}/import?i={quote(code)}")

    def import_result(self, code: str) -> Any:
        return self._get(f"{BASE}/import/result?i={quote(code)}")

    def import_products(self, payload: list[dict[str, Any]]) -> dict[str, Any]:
        response = self.client.post(
            f"{BASE}/import",
            content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "text/plain"},
        )
        try:
            body = response.json()
        except ValueError:
            body = {"text": response.text[:2000]}
        return {
            "status_code": response.status_code,
            "accepted": response.is_success,
            "body": body,
        }
