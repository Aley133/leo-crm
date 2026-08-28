from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any, Iterable
from urllib.parse import urljoin

PRICE_RE = re.compile(r"([0-9][0-9\s\u00a0]*)(?:[.,]\d+)?\s*(₸|₽|KZT|RUB|руб(?:\.|лей|ля)?)", re.I)
NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
REVIEWS_RE = re.compile(r"(\d[\d\s]*)\s*(?:отзыв|оцен)", re.I)
INSTALLMENT_MARKERS = ("×", " x ", "мес", "месяц", "installment", "monthly", "permonth", "в месяц", "рассроч")
PRIMARY_MARKERS = ("primary", "current", "final", "cardprice", "mainprice", "saleprice")
SECONDARY_MARKERS = ("secondary", "old", "original", "strike", "cross", "discount")


def _jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except Exception:
        return value


def _walk(value: Any) -> Iterable[Any]:
    value = _jsonish(value)
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _walk_context(value: Any, path: str = "") -> Iterable[tuple[str, Any, str]]:
    value = _jsonish(value)
    if isinstance(value, dict):
        meta = " ".join(
            str(v) for k, v in value.items()
            if k.lower() in {"textstyle", "style", "type", "kind", "id", "name"} and isinstance(v, (str, int, float))
        )
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if isinstance(child, str):
                yield child_path, child, meta
            yield from _walk_context(child, child_path)
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            child_path = f"{path}[{idx}]"
            if isinstance(child, str):
                yield child_path, child, ""
            yield from _walk_context(child, child_path)


def _first_string(obj: Any, keys: set[str]) -> str | None:
    wanted = {x.lower() for x in keys}
    for node in _walk(obj):
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in wanted and isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def _find_main_state(item: dict[str, Any], wanted: set[str]) -> Any:
    states = item.get("mainState")
    if not isinstance(states, list):
        return None
    for state in states:
        if not isinstance(state, dict):
            continue
        if str(state.get("id") or "").lower() in wanted:
            return state.get("atom") or state
    return None


def _text_from_atom(atom: Any) -> str | None:
    if isinstance(atom, str):
        return atom.strip() or None
    preferred = {"text", "title", "name", "label", "content"}
    for node in _walk(atom):
        if isinstance(node, dict):
            for key in preferred:
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def _all_strings(obj: Any) -> list[str]:
    out: list[str] = []
    for node in _walk(obj):
        if isinstance(node, str) and node.strip():
            out.append(node.strip())
    return out


def _currency_code(token: str | None) -> tuple[str | None, str | None]:
    raw = str(token or "").strip().upper()
    if not raw:
        return None, None
    if "₸" in raw or raw == "KZT":
        return "KZT", "₸"
    if "₽" in raw or raw == "RUB" or raw.startswith("РУБ"):
        return "RUB", "₽"
    return None, None


def _parse_amount(raw: str) -> int | None:
    digits = re.sub(r"\D", "", raw or "")
    return int(digits) if digits else None


CHEAPER_MARKERS = ("есть дешевле", "дешевле", "cheaper", "lower price", "lowerprice")

def _local_strings(value: Any, depth: int = 2) -> list[str]:
    out: list[str] = []
    def walk(v: Any, d: int) -> None:
        v = _jsonish(v)
        if isinstance(v, str):
            if v.strip():
                out.append(v.strip())
            return
        if d <= 0:
            return
        if isinstance(v, dict):
            for k, child in v.items():
                if isinstance(k, str) and any(x in k.lower() for x in ("cheaper", "lowerprice", "minprice")):
                    out.append(k)
                walk(child, d - 1)
        elif isinstance(v, list):
            for child in v[:16]:
                walk(child, d - 1)
    walk(value, depth)
    return out

def _walk_dicts(value: Any, path: str = "") -> Iterable[tuple[str, dict[str, Any]]]:
    value = _jsonish(value)
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from _walk_dicts(child, child_path)
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            child_path = f"{path}[{idx}]"
            yield from _walk_dicts(child, child_path)

def parse_other_seller_offers(payload: Any, base: str = "https://ozon.kz", expected_currency: str | None = "KZT") -> dict[str, Any]:
    """Parse Ozon `/modal/otherOffersFromSellers?sort=price` JSON.

    The modal has a stable `widgetStates.webSellerList-*` state whose value is
    itself a JSON string.  We intentionally parse that seller list directly
    instead of guessing from product-page text such as "Есть дешевле".
    """
    expected = str(expected_currency or "").upper() or None
    root = _jsonish(payload)
    widget_states = root.get("widgetStates") if isinstance(root, dict) else None
    if not isinstance(widget_states, dict):
        return {"offers": [], "cheapest": None, "widget_key": None}

    seller_state = None
    seller_key = None
    for key, value in widget_states.items():
        if str(key).lower().startswith("websellerlist"):
            decoded = _jsonish(value)
            if isinstance(decoded, dict):
                seller_state = decoded
                seller_key = str(key)
                break
    if not isinstance(seller_state, dict):
        return {"offers": [], "cheapest": None, "widget_key": None}

    offers: list[dict[str, Any]] = []
    for seller in list(seller_state.get("sellers") or []):
        if not isinstance(seller, dict):
            continue
        price_obj = seller.get("price") if isinstance(seller.get("price"), dict) else {}
        price_text = str(price_obj.get("price") or "").strip()
        m = PRICE_RE.search(price_text)
        amount = None
        code = symbol = None
        if m:
            amount = _parse_amount(m.group(1))
            code, symbol = _currency_code(m.group(2))
        if expected and code and code != expected:
            continue

        delivery_text = None
        for advantage in list(seller.get("advantages") or []):
            if not isinstance(advantage, dict) or str(advantage.get("key") or "").lower() != "delivery":
                continue
            content_rs = advantage.get("contentRs") if isinstance(advantage.get("contentRs"), dict) else {}
            for atom in list(content_rs.get("headRs") or []):
                if isinstance(atom, dict) and str(atom.get("content") or "").strip():
                    delivery_text = str(atom.get("content")).strip()
                    break
            if delivery_text:
                break
        delivery_date, delivery_days = _delivery_date_from_text(delivery_text or "")

        rating_obj = seller.get("rating") if isinstance(seller.get("rating"), dict) else {}
        product_link = str(seller.get("productLink") or "").strip()
        seller_link = str(seller.get("link") or "").strip()
        offers.append({
            "offer_sku": str(seller.get("sku") or "").strip() or None,
            "seller_id": str(seller.get("id") or "").strip() or None,
            "seller_name": str(seller.get("name") or "").strip() or None,
            "seller_url": urljoin(base.rstrip("/") + "/", seller_link.lstrip("/")) if seller_link else None,
            "product_url": urljoin(base.rstrip("/") + "/", product_link.lstrip("/")) if product_link else None,
            "price_kzt": int(amount) if amount and code == "KZT" else None,
            "price_value": int(amount) if amount else None,
            "price_text": price_text or None,
            "currency_code": code,
            "currency_symbol": symbol,
            "original_price_text": str(price_obj.get("originalPrice") or "").strip() or None,
            "discount_text": str(price_obj.get("discount") or "").strip() or None,
            "delivery_text": delivery_text,
            "delivery_date": delivery_date,
            "delivery_days": delivery_days,
            "authorized_seller": str(seller.get("authorizedSeller") or "").strip() or None,
            "seller_rating": rating_obj.get("totalScore"),
            "seller_reviews": rating_obj.get("reviewsCount"),
        })

    offers.sort(key=lambda x: (
        x.get("price_kzt") if isinstance(x.get("price_kzt"), int) else 10**18,
        x.get("delivery_days") if isinstance(x.get("delivery_days"), int) else 10**9,
        -(float(x.get("seller_rating") or 0)),
    ))
    cheapest = next((dict(x) for x in offers if isinstance(x.get("price_kzt"), int) and x["price_kzt"] > 0), None)
    return {
        "offers": offers,
        "cheapest": cheapest,
        "widget_key": seller_key,
        "offer_count": len(offers),
    }


def cheaper_price_hint(obj: Any, expected_currency: str | None = "KZT") -> dict[str, Any]:
    """Best-effort extraction of Ozon's 'Есть дешевле от ...' price.

    The marker and the amount are often sibling fields inside one widget, so we
    inspect small local subtrees rather than requiring them to be in one string.
    This is deliberately conservative: without a cheaper/lower marker we return
    no hint instead of treating an arbitrary crossed/instalment price as cheaper.
    """
    expected = str(expected_currency or "").upper() or None
    found: list[dict[str, Any]] = []
    for order, (path, node) in enumerate(_walk_dicts(obj)):
        texts = _local_strings(node, depth=2)
        if not texts:
            continue
        joined = " | ".join(texts)
        low = joined.lower().replace("ё", "е")
        if not any(marker in low for marker in CHEAPER_MARKERS):
            continue
        for m in PRICE_RE.finditer(joined):
            amount = _parse_amount(m.group(1))
            code, symbol = _currency_code(m.group(2))
            if not amount or not code:
                continue
            if expected and code != expected:
                continue
            # Instalment amounts are never a valid cheaper seller price.
            around = joined[max(0, m.start()-60): min(len(joined), m.end()+60)].lower()
            if any(marker in around for marker in INSTALLMENT_MARKERS):
                continue
            found.append({
                "value": amount,
                "currency_code": code,
                "currency_symbol": symbol,
                "text": m.group(0).strip(),
                "source": path,
                "context": joined[:260],
                "order": order,
            })
    if not found:
        return {"value": None, "currency_code": None, "currency_symbol": None, "text": None, "source": None}
    found.sort(key=lambda x: (x["value"], x["order"]))
    best = dict(found[0])
    best["candidate_count"] = len(found)
    return best


def _price(item: dict[str, Any], expected_currency: str | None = None) -> dict[str, Any]:
    expected = str(expected_currency or "").upper() or None
    atom = _find_main_state(item, {"price", "pricev2"})
    scope = atom if atom is not None else item
    candidates: list[dict[str, Any]] = []

    for order, (path, text, meta) in enumerate(_walk_context(scope)):
        for match in PRICE_RE.finditer(text):
            amount = _parse_amount(match.group(1))
            code, symbol = _currency_code(match.group(2))
            if not amount or not code:
                continue
            ctx = f"{path} {meta} {text}".lower()
            score = 100.0
            if expected:
                score += 80.0 if code == expected else -80.0
            if "price" in ctx:
                score += 12.0
            if any(marker in ctx for marker in PRIMARY_MARKERS):
                score += 45.0
            if any(marker in ctx for marker in SECONDARY_MARKERS):
                score -= 25.0
            if any(marker in ctx for marker in INSTALLMENT_MARKERS):
                score -= 140.0
            # Stable tie-break: earlier fields win, but only slightly.
            score -= order * 0.001
            candidates.append({
                "value": amount,
                "text": text.strip(),
                "currency_code": code,
                "currency_symbol": symbol,
                "source": path,
                "score": score,
            })

    if candidates:
        candidates.sort(key=lambda x: x["score"], reverse=True)
        best = candidates[0]
        best["candidate_count"] = len(candidates)
        return best

    # Numeric fallback is allowed only when Ozon also exposes an explicit currency code.
    currency_raw = _first_string(item, {"currency", "currencyCode", "currency_code"})
    code, symbol = _currency_code(currency_raw)
    for key in ("finalPrice", "cardPrice", "priceValue", "price"):
        value = item.get(key)
        if isinstance(value, (int, float)) and value > 0 and code:
            return {
                "value": int(value),
                "text": f"{int(value):,} {symbol}".replace(",", " "),
                "currency_code": code,
                "currency_symbol": symbol,
                "source": key,
                "score": 10.0,
                "candidate_count": 1,
            }
    return {
        "value": None,
        "text": None,
        "currency_code": None,
        "currency_symbol": None,
        "source": None,
        "score": None,
        "candidate_count": 0,
    }


def _rating_reviews(item: dict[str, Any]) -> tuple[float | None, int | None]:
    rating = None
    reviews = None
    for key in ("rating", "ratingScore", "score"):
        value = item.get(key)
        if isinstance(value, (int, float)) and 0 <= float(value) <= 5:
            rating = float(value)
            break
    for key in ("reviews", "reviewsCount", "ratingCount", "feedbacks"):
        value = item.get(key)
        if isinstance(value, int) and value >= 0:
            reviews = value
            break
    atom = _find_main_state(item, {"rating", "reviews", "ratingandreviews"})
    strings = _all_strings(atom if atom is not None else item)
    if rating is None:
        for text in strings:
            for raw in NUM_RE.findall(text):
                try:
                    num = float(raw.replace(",", "."))
                except ValueError:
                    continue
                if 0 < num <= 5 and ("★" in text or "рейтинг" in text.lower() or "," in raw or "." in raw):
                    rating = num
                    break
            if rating is not None:
                break
    if reviews is None:
        for text in strings:
            m = REVIEWS_RE.search(text)
            if m:
                reviews = int(re.sub(r"\D", "", m.group(1)))
                break
    return rating, reviews


def _images(item: dict[str, Any]) -> list[str]:
    out: list[str] = []
    def add(value: Any) -> None:
        if not isinstance(value, str) or not value.startswith("http"):
            return
        low = value.lower()
        if not any(x in low for x in ("ozon", "ozone", "ozonusercontent", "ir.ozone")):
            return
        if value not in out:
            out.append(value)

    tile = item.get("tileImage")
    if tile:
        for node in _walk(tile):
            if isinstance(node, dict):
                for key in ("link", "src", "url"):
                    add(node.get(key))
            elif isinstance(node, str):
                add(node)
    if len(out) < 4:
        for node in _walk(item):
            if isinstance(node, str):
                add(node)
                if len(out) >= 6:
                    break
    return out[:6]


def _image(item: dict[str, Any]) -> str | None:
    images = _images(item)
    return images[0] if images else None



MONTHS_RU = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
DATE_RU_RE = re.compile(r"\b(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\b", re.I)
DELIVERY_WORDS = ("достав", "delivery", "получ", "привез", "завтра", "сегодня", "послезавтра", "pickup")


def _delivery_date_from_text(text: str, today: date | None = None) -> tuple[str | None, int | None]:
    today = today or date.today()
    low = str(text or "").lower().replace("ё", "е")
    if "послезавтра" in low:
        target = today + timedelta(days=2)
        return target.isoformat(), 2
    if "завтра" in low:
        target = today + timedelta(days=1)
        return target.isoformat(), 1
    if "сегодня" in low:
        return today.isoformat(), 0
    m = DATE_RU_RE.search(low)
    if not m:
        return None, None
    day = int(m.group(1))
    month = MONTHS_RU[m.group(2)]
    year = today.year
    try:
        target = date(year, month, day)
    except ValueError:
        return None, None
    if target < today - timedelta(days=3):
        try:
            target = date(year + 1, month, day)
        except ValueError:
            return None, None
    return target.isoformat(), (target - today).days


def _delivery(item: dict[str, Any]) -> dict[str, Any]:
    candidates: list[tuple[float, str, str]] = []
    for order, (path, text, meta) in enumerate(_walk_context(item)):
        raw = str(text or "").strip()
        if not raw or len(raw) > 180:
            continue
        low = raw.lower().replace("ё", "е")
        has_date = bool(DATE_RU_RE.search(low)) or any(x in low for x in ("сегодня", "завтра", "послезавтра"))
        if not has_date:
            continue
        ctx = f"{path} {meta} {low}".lower()
        score = 0.0
        if any(x in ctx for x in DELIVERY_WORDS):
            score += 70.0
        if any(x in path.lower() for x in ("delivery", "date", "cart", "button", "eta")):
            score += 55.0
        if "mainstate" in path.lower():
            score += 12.0
        # Date-like labels inside product cards are useful even when the field is unnamed.
        score += 20.0
        score -= order * 0.001
        candidates.append((score, raw, path))
    if not candidates:
        return {"text": None, "date": None, "days": None, "source": None}
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, text, path = candidates[0]
    iso, days = _delivery_date_from_text(text)
    return {"text": text, "date": iso, "days": days, "source": path}


def _brand(item: dict[str, Any]) -> str | None:
    for key in ("brand", "brandName", "brand_name"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for child in ("name", "title", "text"):
                v = value.get(child)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    atom = _find_main_state(item, {"brand"})
    text = _text_from_atom(atom)
    return text.strip() if isinstance(text, str) and text.strip() else None


def _title(item: dict[str, Any]) -> str:
    for key in ("title", "name"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    atom = _find_main_state(item, {"name", "title"})
    text = _text_from_atom(atom)
    if text:
        return text
    return _first_string(item, {"title", "name"}) or "Без названия"


def _url(item: dict[str, Any], base: str) -> str | None:
    candidates: list[str] = []
    for key in ("link", "url"):
        value = item.get(key)
        if isinstance(value, str):
            candidates.append(value)
    action = item.get("action")
    if isinstance(action, dict):
        for key in ("link", "url"):
            value = action.get(key)
            if isinstance(value, str):
                candidates.append(value)
    for node in _walk(item):
        if isinstance(node, str) and "/product/" in node:
            candidates.append(node)
    for value in candidates:
        if "/product/" in value:
            return urljoin(base + "/", value)
    return None


def _sku(item: dict[str, Any]) -> str | None:
    for key in ("sku", "skuId", "id"):
        value = item.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return None


def _candidate_lists(payload: dict[str, Any]) -> list[tuple[str, list[Any]]]:
    out: list[tuple[str, list[Any]]] = []
    widget_states = payload.get("widgetStates")
    if isinstance(widget_states, dict):
        keys = sorted(widget_states.keys(), key=lambda k: ("tilegriddesktop" not in k.lower(), "searchresults" not in k.lower(), k))
        for key in keys:
            parsed = _jsonish(widget_states[key])
            if isinstance(parsed, dict):
                items = parsed.get("items")
                if isinstance(items, list):
                    out.append((key, items))
    if not out:
        for node in _walk(payload):
            if isinstance(node, dict) and isinstance(node.get("items"), list):
                items = node["items"]
                if any(isinstance(x, dict) and _sku(x) for x in items):
                    out.append(("recursive-items", items))
    return out


def parse_search(
    payload: dict[str, Any],
    base: str = "https://www.ozon.ru",
    max_results: int = 48,
    expected_currency: str | None = "KZT",
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    widget_keys: list[str] = []
    for widget_key, items in _candidate_lists(payload):
        widget_keys.append(widget_key)
        for raw in items:
            if not isinstance(raw, dict):
                continue
            sku = _sku(raw)
            if not sku or sku in seen:
                continue
            title = _title(raw)
            url = _url(raw, base)
            if title == "Без названия" and not url:
                continue
            price = _price(raw, expected_currency=expected_currency)
            cheaper = cheaper_price_hint(raw, expected_currency=expected_currency)
            rating, reviews = _rating_reviews(raw)
            delivery = _delivery(raw)
            value = price.get("value")
            currency_code = price.get("currency_code")
            cheaper_value = cheaper.get("value") if cheaper.get("currency_code") == "KZT" else None
            effective_kzt = None
            if currency_code == "KZT" and value is not None:
                effective_kzt = int(value)
            if cheaper_value is not None:
                effective_kzt = min(effective_kzt, int(cheaper_value)) if effective_kzt is not None else int(cheaper_value)
            row = {
                "sku": sku,
                "title": title,
                "brand": _brand(raw),
                "price_value": value,
                "price_text": price.get("text"),
                "currency_code": currency_code,
                "currency_symbol": price.get("currency_symbol"),
                "price_kzt": value if currency_code == "KZT" else None,
                "price_rub": value if currency_code == "RUB" else None,
                "cheaper_price_kzt": cheaper_value,
                "cheaper_price_text": cheaper.get("text"),
                "cheaper_price_source": cheaper.get("source"),
                "effective_price_kzt": effective_kzt,
                "price_source": price.get("source"),
                "price_candidate_count": price.get("candidate_count"),
                "rating": rating,
                "reviews": reviews,
                "delivery_text": delivery.get("text"),
                "delivery_date": delivery.get("date"),
                "delivery_days": delivery.get("days"),
                "delivery_source": delivery.get("source"),
                "image_url": _image(raw),
                "image_urls": _images(raw),
                "ozon_url": url,
                "widget_key": widget_key,
            }
            rows.append(row)
            seen.add(sku)
            if len(rows) >= max_results:
                return {"items": rows, "widget_keys": widget_keys, "parser": "widgetStates"}
    return {"items": rows, "widget_keys": widget_keys, "parser": "widgetStates"}


def parse_product_page(
    payload: dict[str, Any],
    base: str = "https://www.ozon.kz",
    expected_currency: str | None = "KZT",
) -> dict[str, Any]:
    """Read the displayed price from an exact Ozon product-page payload.

    Product pages split their heading, gallery, delivery and price across
    separate widget states, so they cannot be parsed as search-card lists.  We
    deliberately inspect only price-named widgets and still require an
    explicit expected currency.  This keeps instalment amounts and unrelated
    recommendations from becoming a supplier cost.
    """

    root = _jsonish(payload)
    widget_states = root.get("widgetStates") if isinstance(root, dict) else None
    if not isinstance(widget_states, dict):
        return {
            "price_kzt": None,
            "price_value": None,
            "price_text": None,
            "currency_code": None,
            "price_source": None,
            "widget_key": None,
        }

    candidates: list[dict[str, Any]] = []
    page_states: list[tuple[str, Any]] = []
    for order, (key, value) in enumerate(widget_states.items()):
        decoded = _jsonish(value)
        if not isinstance(decoded, (dict, list)):
            continue
        page_states.append((str(key), decoded))
        key_lower = str(key).lower()
        # Exact page price lives in webPrice. Recommendation and carousel
        # widgets also contain "price" but belong to other products.
        if not key_lower.startswith("webprice"):
            continue
        scope = decoded if isinstance(decoded, dict) else {"items": decoded}
        price = _price(scope, expected_currency=expected_currency)
        amount = price.get("value")
        currency = price.get("currency_code")
        price_context = f"{price.get('source') or ''} {price.get('text') or ''}".lower()
        if not isinstance(amount, int) or amount <= 0 or currency != str(expected_currency or "").upper():
            continue
        if any(marker in price_context for marker in SECONDARY_MARKERS + INSTALLMENT_MARKERS):
            continue
        candidates.append({
            **price,
            "widget_key": str(key),
            "rank": 200.0 + float(price.get("score") or 0) - (order * 0.001),
        })

    if not candidates:
        return {
            "price_kzt": None,
            "price_value": None,
            "price_text": None,
            "currency_code": None,
            "price_source": None,
            "widget_key": None,
        }

    candidates.sort(key=lambda row: row["rank"], reverse=True)
    best = candidates[0]
    combined = {key: value for key, value in page_states}
    delivery = _delivery(combined)
    rating, reviews = _rating_reviews(combined)
    return {
        "price_kzt": best["value"],
        "price_value": best["value"],
        "price_text": best.get("text"),
        "currency_code": best.get("currency_code"),
        "currency_symbol": best.get("currency_symbol"),
        "price_source": f"{best['widget_key']}.{best.get('source') or 'price'}",
        "price_candidate_count": len(candidates),
        "widget_key": best["widget_key"],
        "delivery_text": delivery.get("text"),
        "delivery_date": delivery.get("date"),
        "delivery_days": delivery.get("days"),
        "rating": rating,
        "reviews": reviews,
        "base": base,
    }
