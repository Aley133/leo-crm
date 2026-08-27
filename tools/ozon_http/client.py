from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import Config, ROOT
from .parser import parse_search


class OzonHttpClient:
    """READ ONLY Ozon HTTP probe. Never launches a browser."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config.load()
        try:
            from curl_cffi import requests as curl_requests
        except Exception as exc:  # pragma: no cover - user runtime dependency
            raise RuntimeError("Не установлен curl_cffi. Запусти RUN_UI.cmd ещё раз.") from exc
        self._requests = curl_requests
        self.session = curl_requests.Session(impersonate=self.config.impersonate)
        self.bootstrapped = False

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    @staticmethod
    def _inner_url(query: str, page: int) -> str:
        text = quote(query.strip(), safe="")
        return f"/search/?text={text}&page={max(1, int(page))}&from_global=true&deny_category_prediction=true"

    def _headers(self, query: str) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": f"https://www.ozon.ru/search/?text={quote(query.strip(), safe='')}",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    def _bootstrap(self) -> dict[str, Any]:
        if self.bootstrapped or not self.config.bootstrap:
            return {"attempted": False, "ok": self.bootstrapped, "reason": "disabled_or_done"}
        started = time.perf_counter()
        try:
            resp = self.session.get(
                "https://www.ozon.ru/",
                headers={"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
                timeout=self.config.timeout,
                allow_redirects=True,
            )
            self.bootstrapped = resp.status_code < 400
            return {
                "attempted": True,
                "ok": self.bootstrapped,
                "status_code": resp.status_code,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "final_url": str(resp.url),
                "content_type": resp.headers.get("content-type"),
                "bytes": len(resp.content or b""),
            }
        except Exception as exc:
            return {"attempted": True, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _strategies(self, query: str, page: int) -> list[tuple[str, str, dict[str, str]]]:
        inner = self._inner_url(query, page)
        return [
            ("entrypoint_www", "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2", {"url": inner}),
            ("composer_www", "https://www.ozon.ru/api/composer-api.bx/page/json/v2", {"url": inner}),
            ("composer_api", "https://api.ozon.ru/composer-api.bx/page/json/v2", {"url": inner}),
        ]

    def search(self, query: str, page: int = 1, strategy: str = "auto") -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            raise ValueError("Введи название товара для поиска на Ozon")
        page = max(1, int(page))
        bootstrap = self._bootstrap()
        attempts: list[dict[str, Any]] = []
        selected = self._strategies(query, page)
        if strategy != "auto":
            selected = [x for x in selected if x[0] == strategy]
            if not selected:
                raise ValueError(f"Неизвестная HTTP-стратегия: {strategy}")

        for name, url, params in selected:
            started = time.perf_counter()
            try:
                resp = self.session.get(
                    url,
                    params=params,
                    headers=self._headers(query),
                    timeout=self.config.timeout,
                    allow_redirects=True,
                )
                elapsed = round((time.perf_counter() - started) * 1000, 1)
                content_type = (resp.headers.get("content-type") or "").lower()
                text_head = (resp.text or "")[:500]
                attempt: dict[str, Any] = {
                    "strategy": name,
                    "url": url,
                    "params": params,
                    "status_code": resp.status_code,
                    "elapsed_ms": elapsed,
                    "content_type": content_type,
                    "bytes": len(resp.content or b""),
                    "final_url": str(resp.url),
                    "blocked": resp.status_code in {401, 403, 429} or "captcha" in text_head.lower() or "variti" in text_head.lower(),
                }
                if resp.status_code != 200:
                    attempt["body_preview"] = text_head.replace("\n", " ")[:260]
                    attempts.append(attempt)
                    continue
                try:
                    payload = resp.json()
                except Exception:
                    attempt["body_preview"] = text_head.replace("\n", " ")[:260]
                    attempt["json"] = False
                    attempts.append(attempt)
                    continue
                if not isinstance(payload, dict):
                    attempt["json"] = True
                    attempt["payload_type"] = type(payload).__name__
                    attempts.append(attempt)
                    continue

                parsed = parse_search(payload, expected_currency=self.config.expected_currency, base="https://www.ozon.ru", max_results=self.config.max_results)
                attempt["json"] = True
                attempt["parsed_items"] = len(parsed["items"])
                attempt["widget_keys"] = parsed["widget_keys"][:12]
                attempts.append(attempt)

                # Save 200 JSON even if parser found zero: invaluable for schema drift.
                data_dir = ROOT / "data"
                data_dir.mkdir(parents=True, exist_ok=True)
                (data_dir / "last_ozon_response.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

                if parsed["items"]:
                    return {
                        "ok": True,
                        "query": query,
                        "page": page,
                        "strategy_used": name,
                        "bootstrap": bootstrap,
                        "attempts": attempts,
                        "items": parsed["items"],
                        "parser": parsed["parser"],
                        "widget_keys": parsed["widget_keys"],
                        "response_saved": "data/last_ozon_response.json",
                    }
                # A healthy 200 with widgetStates but no parsed items is parser drift, not proof of no results.
                if payload.get("widgetStates"):
                    return {
                        "ok": False,
                        "reason": "parser_drift",
                        "query": query,
                        "page": page,
                        "strategy_used": name,
                        "bootstrap": bootstrap,
                        "attempts": attempts,
                        "items": [],
                        "widget_keys": list(payload.get("widgetStates", {}).keys())[:40] if isinstance(payload.get("widgetStates"), dict) else [],
                        "response_saved": "data/last_ozon_response.json",
                        "note": "Ozon вернул JSON, но структура поискового виджета изменилась. Пришли last_ozon_response.json.",
                    }
            except Exception as exc:
                attempts.append({
                    "strategy": name,
                    "url": url,
                    "params": params,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                })

        blocked = any(x.get("blocked") for x in attempts)
        return {
            "ok": False,
            "reason": "blocked" if blocked else "transport_failed",
            "query": query,
            "page": page,
            "bootstrap": bootstrap,
            "attempts": attempts,
            "items": [],
            "note": (
                "Ozon не пропустил чистый HTTP (403/anti-bot). Следующий эксперимент — снять один HAR поиска Ozon и повторить точные headers/cookies без Playwright."
                if blocked else
                "Ни одна HTTP-стратегия не вернула пригодный JSON. Смотри диагностику."
            ),
        }
