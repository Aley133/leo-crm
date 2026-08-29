from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from tools.ozon_http.config import Config as OzonConfig
from tools.ozon_http.resolver import OzonSessionResolver
from tools.ozon_http.session_profile import CurlProfile

from .mapper import build_payload, map_characteristics, validate_payload
from .official_api import OfficialProductsApi
from .parser import (
    build_semantic_description,
    enrich_supplement_characteristics,
    extract_logistics_weight_kg,
    parse_ozon_response,
    suggest_kaspi_category_query,
)


class NewCardImportRejected(RuntimeError):
    """Kaspi finished Product Import, but detailed validation rejected the card."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _list_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [value for value in payload if isinstance(value, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "content", "values", "categories", "attributes"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _normalize_category(row: dict[str, Any]) -> dict[str, str] | None:
    code = _text(row.get("code") or row.get("id"))
    title = _text(row.get("title") or row.get("name") or row.get("label"))
    return {"code": code, "title": title} if code and title else None


def _normalize_attribute(row: dict[str, Any]) -> dict[str, Any]:
    code = _text(row.get("code") or row.get("id"))
    return {
        "code": code,
        "title": _text(row.get("title") or row.get("name") or row.get("label")) or code,
        "required": bool(
            row.get("mandatory")
            or row.get("required")
            or row.get("isRequired")
            or row.get("isMandatory")
        ),
        "type": _text(row.get("type") or row.get("valueType")).lower(),
        "multi_valued": bool(row.get("multiValued") or row.get("multi_valued")),
    }


def _normalize_values(raw: Any) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for row in _list_payload(raw):
        code = _text(row.get("code") or row.get("value") or row.get("id"))
        name = _text(row.get("name") or row.get("title") or row.get("label") or code)
        if code or name:
            output.append({"code": code or name, "name": name or code})
    return output[:300]


def _norm(value: Any) -> str:
    return re.sub(r"[^a-zа-я0-9]+", " ", _text(value).casefold().replace("ё", "е")).strip()


def _category_score(category: dict[str, str], hint: str) -> tuple[int, int, str]:
    title = _norm(category.get("title"))
    wanted = _norm(hint)
    if not wanted:
        return (0, 0, title)
    exact = int(title == wanted)
    contains = int(wanted in title or title in wanted)
    overlap = len(set(title.split()) & set(wanted.split()))
    return (exact * 100 + contains * 20 + overlap, -abs(len(title) - len(wanted)), title)


def _load_mapping(
    api: OfficialProductsApi,
    category: str,
    characteristics: list[dict[str, str]],
) -> list[dict[str, Any]]:
    attributes = [
        normalized
        for row in _list_payload(api.attributes(category))
        if (normalized := _normalize_attribute(row)).get("code")
    ]
    values_by_code: dict[str, list[dict[str, str]]] = {}

    def load_values(code: str) -> tuple[str, list[dict[str, str]]]:
        try:
            return code, _normalize_values(api.attribute_values(category, code))
        except Exception:
            return code, []

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(attributes)))) as pool:
        futures = [pool.submit(load_values, str(row["code"])) for row in attributes]
        for future in as_completed(futures):
            code, values = future.result()
            if values:
                values_by_code[code] = values
    return map_characteristics(characteristics, attributes, values_by_code)


class OzonProductCardClient:
    """Read a full Ozon product card through the existing encrypted HTTP session."""

    def __init__(self, profile: CurlProfile, config: OzonConfig | None = None) -> None:
        self.profile = profile
        self.config = config or OzonConfig.load()
        try:
            from curl_cffi import requests as curl_requests
        except Exception as exc:  # pragma: no cover - packaging guard
            raise RuntimeError("curl_cffi is not installed") from exc
        self.session = curl_requests.Session(impersonate=self.config.impersonate)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    @staticmethod
    def _blocked(status_code: int, text_head: str) -> bool:
        low = _text(text_head).casefold()
        return status_code in {401, 403, 429, 451} or any(
            marker in low for marker in ("captcha", "incidentid", "access denied", "supporturl")
        )

    def _fetch_once(self, product_url: str) -> tuple[dict[str, Any], dict[str, Any]]:
        target_url = self.profile.rewritten_page_url(product_url)
        started = time.perf_counter()
        response = self.session.get(
            target_url,
            headers=self.profile.request_headers_for_page(product_url),
            timeout=self.config.timeout,
            allow_redirects=True,
        )
        content = bytes(response.content or b"")
        head = content[:1600].decode("utf-8", errors="replace")
        blocked = self._blocked(int(response.status_code), head)
        cookie_updates: dict[str, str] = {}
        for jar in (getattr(response, "cookies", None), getattr(self.session, "cookies", None)):
            try:
                values = jar.get_dict() if jar is not None else {}
            except Exception:
                values = {}
            if isinstance(values, dict):
                cookie_updates.update({str(key): str(value) for key, value in values.items()})
        changed = self.profile.merge_cookie_values(cookie_updates)
        attempt = {
            "status_code": int(response.status_code),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "blocked": blocked,
            "bytes": len(content),
            "content_type": _text(response.headers.get("content-type")),
            "final_url": str(response.url),
            "cookie_updates_seen": len(cookie_updates),
            "cookie_values_changed": changed,
            "transport": "saved_session_http",
        }
        if int(response.status_code) != 200 or blocked:
            return {
                "ok": False,
                "source_url": product_url,
                "title": None,
                "brand": None,
                "description": None,
                "characteristics": [],
                "images": [],
                "error": "Ozon session request was blocked or did not return HTTP 200",
            }, attempt
        return parse_ozon_response(content, attempt["content_type"], product_url), attempt

    @staticmethod
    def _features_url(product_url: str) -> str:
        parts = urlsplit(_text(product_url))
        path = parts.path.rstrip("/")
        path = path + "/" if path.endswith("/features") else path + "/features/"
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    @staticmethod
    def _merge_characteristics(
        main_rows: list[dict[str, Any]], feature_rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        by_name: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for row in [*main_rows, *feature_rows]:
            name = _text(row.get("name"))
            value = _text(row.get("value"))
            if not name or not value:
                continue
            key = name.casefold().replace("ё", "е")
            if key not in by_name:
                order.append(key)
            by_name[key] = dict(row)
        return [by_name[key] for key in order][:180]

    def fetch(self, product_url: str) -> dict[str, Any]:
        parsed, attempt = self._fetch_once(product_url)
        if not parsed.get("ok"):
            parsed["attempt"] = attempt
            parsed["transport_mode"] = "saved_session_http"
            return parsed
        features_attempt: dict[str, Any] | None = None
        if parsed.get("category_hint") or len(parsed.get("characteristics") or []) < 8:
            try:
                feature_card, features_attempt = self._fetch_once(self._features_url(product_url))
                if feature_card.get("ok") or feature_card.get("characteristics"):
                    merged = self._merge_characteristics(
                        list(parsed.get("characteristics") or []),
                        list(feature_card.get("characteristics") or []),
                    )
                    parsed["characteristics"] = enrich_supplement_characteristics(
                        _text(parsed.get("title") or feature_card.get("title")), merged
                    )
                    parsed["weight_kg"] = extract_logistics_weight_kg(parsed["characteristics"])
                    parsed["category_hint"] = (
                        suggest_kaspi_category_query(_text(parsed.get("title")), parsed["characteristics"])
                        or feature_card.get("category_hint")
                        or parsed.get("category_hint")
                    )
                    parsed["description"] = build_semantic_description(
                        parsed["characteristics"],
                        _text(parsed.get("description_raw") or feature_card.get("description_raw")),
                        title=_text(parsed.get("title") or feature_card.get("title")),
                        brand=_text(parsed.get("brand") or feature_card.get("brand")),
                    )
            except Exception as exc:
                features_attempt = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:240]}
        parsed["attempt"] = attempt
        parsed["features_attempt"] = features_attempt
        parsed["transport_mode"] = "saved_session_http+features"
        return parsed


def map_new_card_category(
    api_token: str,
    *,
    category: str,
    characteristics: list[dict[str, str]],
) -> dict[str, Any]:
    category = _text(category)
    if not category:
        raise ValueError("Выберите категорию Kaspi")
    api = OfficialProductsApi(api_token)
    try:
        mapped = _load_mapping(api, category, characteristics)
    finally:
        api.close()
    return {"category": category, "attributes": mapped}


def prepare_new_card(api_token: str, product_url: str) -> dict[str, Any]:
    profile = OzonSessionResolver().resolve()
    client = OzonProductCardClient(profile)
    try:
        card = client.fetch(product_url)
    finally:
        client.close()
    if not card.get("ok") or not _text(card.get("title")) or not list(card.get("images") or []):
        raise ValueError(_text(card.get("error")) or "Ozon не вернул данные для новой карточки")

    api = OfficialProductsApi(api_token)
    try:
        categories = [
            normalized
            for row in _list_payload(api.categories())
            if (normalized := _normalize_category(row)) is not None
        ]
        hint = _text(card.get("category_hint"))
        ranked = sorted(categories, key=lambda row: _category_score(row, hint), reverse=True)
        selected = ranked[0] if ranked and _category_score(ranked[0], hint)[0] > 0 else None
        mapped = (
            _load_mapping(api, selected["code"], list(card.get("characteristics") or []))
            if selected is not None
            else []
        )
    finally:
        api.close()

    sku_match = re.findall(r"\d{6,}", urlsplit(product_url).path)
    sku = sku_match[-1] if sku_match else ""
    draft = {
        "source_url": product_url,
        "sku": sku[:64],
        "title": _text(card.get("title"))[:1024],
        "brand": _text(card.get("brand"))[:255],
        "description": _text(card.get("description") or card.get("description_raw"))[:1024],
        "weight": card.get("weight_kg"),
        "category": selected["code"] if selected else "",
        "category_title": selected["title"] if selected else "",
        "category_hint": hint,
        "categories": ranked[:1000],
        "attributes": mapped,
        "characteristics": list(card.get("characteristics") or [])[:180],
        "images": [_text(value) for value in list(card.get("images") or [])[:20] if _text(value).startswith("https://")],
        "transport_mode": card.get("transport_mode"),
        "attempt": card.get("attempt") if isinstance(card.get("attempt"), dict) else {},
    }
    errors: list[str] = []
    if draft["category"]:
        product = build_payload(
            sku=draft["sku"],
            title=draft["title"],
            brand=draft["brand"],
            category=draft["category"],
            description=draft["description"],
            attributes=draft["attributes"],
            images=draft["images"][:10],
            weight=draft["weight"],
        )
        errors = validate_payload(product, draft["attributes"])
    else:
        errors = ["Kaspi category is required"]
    draft["validation_errors"] = errors
    return draft


def _find_import_code(value: Any) -> str | None:
    if not isinstance(value, (dict, list)):
        return None
    if isinstance(value, dict):
        for key in ("code", "importCode", "id"):
            candidate = value.get(key)
            if isinstance(candidate, (str, int)) and _text(candidate):
                return _text(candidate)
        children = value.values()
    else:
        children = value
    for child in children:
        found = _find_import_code(child)
        if found:
            return found
    return None


def _import_finished(value: Any) -> bool:
    if isinstance(value, dict):
        if _text(value.get("status") or value.get("state")).upper() == "FINISHED":
            return True
        return any(_import_finished(child) for child in value.values())
    if isinstance(value, list):
        return any(_import_finished(child) for child in value)
    return False


def _detailed_outcome(value: Any) -> tuple[bool, int, list[str]]:
    errors = 0
    failed: list[str] = []

    def walk(node: Any, path: str = "result") -> None:
        nonlocal errors
        if isinstance(node, dict):
            raw_errors = node.get("errors")
            if isinstance(raw_errors, (int, float)):
                errors = max(errors, int(raw_errors))
            elif isinstance(raw_errors, str) and raw_errors.strip().isdigit():
                errors = max(errors, int(raw_errors.strip()))
            state = _text(node.get("state") or node.get("status")).upper()
            if state in {"ERRORS", "ERROR", "REJECTED", "FAILED"}:
                message = _text(node.get("message") or node.get("error") or state)
                failed.append(f"{path}: {message}"[:500])
            for key, child in node.items():
                walk(child, f"{path}.{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{path}[{index}]")

    walk(value)
    return errors == 0 and not failed, errors, failed[:20]


def create_new_card(
    api_token: str,
    draft: dict[str, Any],
    *,
    attempts: int = 180,
    poll_seconds: float = 3.0,
) -> dict[str, Any]:
    attributes = list(draft.get("attributes") or [])
    product = build_payload(
        sku=_text(draft.get("sku")),
        title=_text(draft.get("title")),
        brand=_text(draft.get("brand")),
        category=_text(draft.get("category")),
        description=_text(draft.get("description")),
        attributes=attributes,
        images=[_text(value) for value in list(draft.get("images") or [])[:10]],
        weight=draft.get("weight"),
    )
    validation_errors = validate_payload(product, attributes)
    if validation_errors:
        raise NewCardImportRejected("; ".join(validation_errors))

    api = OfficialProductsApi(api_token)
    try:
        submitted = api.import_products([product])
        if not submitted.get("accepted"):
            raise NewCardImportRejected(
                f"Kaspi Product Import HTTP {submitted.get('status_code')}: {submitted.get('body')}"
            )
        code = _find_import_code(submitted.get("body") or submitted)
        if not code:
            raise NewCardImportRejected("Kaspi принял Product Import, но не вернул import code")
        status: Any = None
        for attempt in range(1, max(1, int(attempts)) + 1):
            status = api.import_status(code)
            if _import_finished(status):
                break
            if attempt < max(1, int(attempts)):
                time.sleep(max(0.5, float(poll_seconds)))
        if not _import_finished(status):
            raise NewCardImportRejected("Kaspi Product Import не завершился в контрольное время")
        detailed = api.import_result(code)
    finally:
        api.close()

    ok, error_count, failed_rows = _detailed_outcome(detailed)
    if not ok:
        detail = "; ".join(failed_rows) or f"errors={error_count}"
        raise NewCardImportRejected(f"Карточка не прошла detailed validation Kaspi: {detail}")
    return {
        "result": "NEW_CARD_ACCEPTED_FOR_MODERATION",
        "import_code": code,
        "sku": product["sku"],
        "title": product["title"],
        "category": product["category"],
        "errors": error_count,
        "detailed_ok": True,
    }
