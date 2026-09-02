from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from tools.kaspi_fast_dumping_scanner import inspect_kaspi_product
from tools.ozon_http.image_verify import ImageVerifier
from tools.ozon_http.matcher import build_search_queries, rank_product
from tools.ozon_http.resolver import OzonSessionResolver
from tools.ozon_http.session_client import OzonSessionHttpClient

from .kaspi_search import KaspiProductSearch

if TYPE_CHECKING:
    from .kaspi_offer_creator import MerchantOfferApi


SellerCountResolver = Callable[[dict[str, Any], str, str, int], tuple[int | None, dict[str, Any]]]


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    digits = "".join(character for character in str(value) if character.isdigit())
    return int(digits) if digits else None


def _rating(value: Any) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _resolve_kaspi_seller_count(
    product: dict[str, Any],
    city_id: str,
    zone_id: str,
    maximum_sellers: int,
) -> tuple[int | None, dict[str, Any]]:
    # Kaspi pages contain five offers per page. Reading one page beyond the
    # configured maximum is enough to distinguish "at most N" from "more than
    # N" without downloading an unbounded seller list. The search-card counter
    # is intentionally not trusted for admission because Kaspi can describe it
    # as either all offers or additional offers in different storefront shapes.
    max_pages = min(20, max(2, math.ceil((maximum_sellers + 1) / 5)))
    inspected = asyncio.run(
        inspect_kaspi_product(
            reference=str(product.get("kaspi_url") or product.get("master_sku") or ""),
            city_id=city_id,
            zone_id=zone_id,
            max_pages=max_pages,
        )
    )
    offers = inspected.get("offers") if isinstance(inspected.get("offers"), dict) else {}
    count = _count(offers.get("seller_count_scanned"))
    if count is None or count <= 0:
        return None, {"seller_count_source": "unavailable"}
    return count, {
        "seller_count_source": "kaspi_offer_pages",
        "seller_count_scanned": count,
        "top_offers": list(offers.get("top_offers") or [])[:10],
    }


def _image_urls(row: dict[str, Any]) -> list[str]:
    values = list(row.get("image_urls") or [])
    if row.get("image_url") and row["image_url"] not in values:
        values.insert(0, row["image_url"])
    return [str(value) for value in values if str(value).startswith("http")]


def _strictly_confirmed(candidate: dict[str, Any]) -> bool:
    return bool(
        candidate.get("match_status") == "CONFIRMED"
        and not candidate.get("hard_mismatch")
        and not candidate.get("brand_conflict")
        and not candidate.get("core_missing")
        and candidate.get("ozon_url")
    )


def _search_card_kzt_price(candidate: dict[str, Any]) -> tuple[int | None, str | None]:
    """Return only an explicitly parsed KZT price from an Ozon search card."""

    for field in ("effective_price_kzt", "price_kzt"):
        value = candidate.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value, field
    return None, None


def _attach_search_card_supplier_offer(candidate: dict[str, Any]) -> bool:
    """Use the exact card price when Ozon's other-sellers modal is unavailable."""

    price, field = _search_card_kzt_price(candidate)
    if price is None or field is None:
        return False
    candidate.update({
        "supplier_price_kzt": price,
        "supplier_url": candidate.get("ozon_url"),
        "supplier_offer_sku": candidate.get("sku"),
        "supplier_seller_name": candidate.get("seller_name"),
        "supplier_delivery_days": candidate.get("delivery_days"),
        "supplier_delivery_text": candidate.get("delivery_text"),
        "supplier_delivery_date": candidate.get("delivery_date"),
        "supplier_seller_rating": candidate.get("seller_rating"),
        "supplier_seller_reviews": candidate.get("seller_reviews"),
        "supplier_price_source": f"search_card.{field}",
    })
    candidate.setdefault("supplier_offer_count", 0)
    return True


def _attach_product_page_supplier_offer(
    client: OzonSessionHttpClient,
    candidate: dict[str, Any],
) -> bool:
    """Last fallback: read `webPrice` from the already matched exact card."""

    reader = getattr(client, "product_page_price", None)
    if not callable(reader) or not candidate.get("ozon_url"):
        return False
    try:
        detail = reader(str(candidate["ozon_url"]), candidate.get("sku"))
    except Exception as exc:
        candidate["product_page_price_error"] = f"{type(exc).__name__}: {exc}"[:500]
        return False
    price = detail.get("price_kzt")
    if not detail.get("ok") or not isinstance(price, int) or isinstance(price, bool) or price <= 0:
        return False
    candidate.update({
        "supplier_price_kzt": price,
        "supplier_url": candidate.get("ozon_url"),
        "supplier_offer_sku": candidate.get("sku") or detail.get("product_id"),
        "supplier_seller_name": candidate.get("seller_name") or "Ozon",
        "supplier_delivery_days": (
            detail.get("delivery_days")
            if detail.get("delivery_days") is not None
            else candidate.get("delivery_days")
        ),
        "supplier_delivery_text": detail.get("delivery_text") or candidate.get("delivery_text"),
        "supplier_delivery_date": detail.get("delivery_date") or candidate.get("delivery_date"),
        "supplier_seller_rating": candidate.get("seller_rating"),
        "supplier_seller_reviews": candidate.get("seller_reviews"),
        "supplier_price_source": f"product_page.{detail.get('price_source') or 'webPrice'}",
    })
    candidate.setdefault("supplier_offer_count", 0)
    return True


def _attach_lowest_supplier_offer(
    client: OzonSessionHttpClient,
    candidate: dict[str, Any],
) -> bool:
    """Attach the cheapest confirmed seller offer for one exact Ozon card."""

    try:
        detail = client.product_price_hints(str(candidate["ozon_url"]))
    except Exception as exc:
        candidate["supplier_lookup_error"] = f"{type(exc).__name__}: {exc}"[:500]
        return (
            _attach_search_card_supplier_offer(candidate)
            or _attach_product_page_supplier_offer(client, candidate)
        )
    offer = detail.get("cheaper_offer") or {}
    price = detail.get("cheaper_price_kzt")
    candidate["supplier_offer_count"] = detail.get("other_offer_count") or 0
    if not detail.get("ok") or not isinstance(price, int) or isinstance(price, bool) or price <= 0:
        return (
            _attach_search_card_supplier_offer(candidate)
            or _attach_product_page_supplier_offer(client, candidate)
        )
    candidate.update({
        "supplier_price_kzt": price,
        "supplier_url": offer.get("product_url") or candidate.get("ozon_url"),
        "supplier_offer_sku": offer.get("offer_sku") or candidate.get("sku"),
        "supplier_seller_name": offer.get("seller_name"),
        "supplier_delivery_days": (
            offer.get("delivery_days")
            if offer.get("delivery_days") is not None
            else candidate.get("delivery_days")
        ),
        "supplier_delivery_text": offer.get("delivery_text") or candidate.get("delivery_text"),
        "supplier_delivery_date": offer.get("delivery_date") or candidate.get("delivery_date"),
        "supplier_seller_rating": offer.get("seller_rating"),
        "supplier_seller_reviews": offer.get("seller_reviews"),
        "supplier_price_source": "otherOffersFromSellers.lowest_confirmed",
    })
    return True


def _product_id_from_ozon_url(url: str) -> str | None:
    path = urlsplit(str(url or "").strip()).path
    values = re.findall(r"\d{6,}", path)
    return values[-1] if values else None


def _manual_url_candidate(
    client: OzonSessionHttpClient,
    url: str,
    *,
    product_id: str | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Resolve the exact Ozon card so a manual URL still has a comparison photo."""

    slug = unquote(urlsplit(url).path.rstrip("/").split("/")[-1])
    slug = re.sub(r"-\d{6,}$", "", slug).replace("-", " ").strip()
    queries = [value for value in (product_id, slug) if value]
    attempts: list[dict[str, Any]] = []
    seen_queries: set[str] = set()
    for query in queries:
        key = str(query).casefold()
        if key in seen_queries:
            continue
        seen_queries.add(key)
        result = client.search(str(query), page=1)
        attempt = result.get("attempt") or {}
        items = list(result.get("items") or [])
        attempts.append({
            "query": query,
            "http_status": attempt.get("status_code"),
            "blocked": bool(attempt.get("blocked")),
            "items": len(items),
        })
        for candidate in items:
            candidate_id = str(candidate.get("sku") or "").strip()
            candidate_url_id = _product_id_from_ozon_url(str(candidate.get("ozon_url") or ""))
            if product_id and product_id in {candidate_id, candidate_url_id}:
                return dict(candidate), attempts
        if attempt.get("blocked"):
            break
    return None, attempts


def _best_ozon_match(
    client: OzonSessionHttpClient,
    product: dict[str, Any],
    *,
    max_queries: int,
    verifier: ImageVerifier | None,
) -> dict[str, Any]:
    merged: dict[str, dict[str, Any]] = {}
    attempts: list[dict[str, Any]] = []
    queries = build_search_queries(product, max_queries=max_queries)
    for query in queries:
        result = client.search(query, page=1)
        attempt = result.get("attempt") or {}
        attempts.append({
            "query": query,
            "http_status": attempt.get("status_code"),
            "blocked": bool(attempt.get("blocked")),
            "items": len(result.get("items") or []),
        })
        for item in result.get("items") or []:
            key = str(item.get("sku") or item.get("ozon_url") or "").strip()
            if key:
                merged.setdefault(key, dict(item))
        if attempt.get("blocked"):
            break

    ranked = rank_product(product, list(merged.values()))
    if verifier is not None:
        for candidate in ranked[:8]:
            if candidate.get("hard_mismatch"):
                continue
            visual = verifier.verify(_image_urls(product), _image_urls(candidate), max_pairs=6)
            candidate["image_match"] = visual
            if (
                visual.get("status") in {"CONFIRM", "SUPPORT"}
                and float(candidate.get("match_score") or 0) >= 0.66
                and not candidate.get("brand_conflict")
                and not candidate.get("core_missing")
            ):
                candidate["match_status"] = "CONFIRMED"
                candidate["match_score"] = min(1.0, float(candidate.get("match_score") or 0) + 0.08)
        ranked.sort(
            key=lambda x: (x.get("match_status") == "CONFIRMED", x.get("match_score") or 0),
            reverse=True,
        )

    confirmed = [candidate for candidate in ranked if _strictly_confirmed(candidate)]
    priced = [
        candidate
        for candidate in confirmed[:8]
        if _attach_lowest_supplier_offer(client, candidate)
    ]
    if priced:
        selected = min(
            priced,
            key=lambda candidate: (
                int(candidate.get("supplier_price_kzt") or 10**18),
                -float(candidate.get("match_score") or 0),
            ),
        )
        best = dict(selected)
        best["selection_reason"] = "lowest_price_across_strict_matches_and_sellers"
    else:
        best = dict(ranked[0]) if ranked else None
    if best:
        best["queries_tested"] = len(attempts)
        best["strict_candidates_checked"] = min(8, len(confirmed))
        best["priced_strict_candidates"] = len(priced)
        best["total_supplier_offers_checked"] = sum(
            int(candidate.get("supplier_offer_count") or 0)
            for candidate in confirmed[:8]
        )
    return {"best": best, "top_candidates": ranked[:5], "queries": attempts}


def discover_products(
    *,
    query: str,
    city_id: str,
    target_new: int,
    max_kaspi_scan: int,
    max_ozon_queries: int,
    image_verify: bool,
    existing_kaspi_ids: set[str] | None = None,
    merchant_catalog: "MerchantOfferApi | None" = None,
) -> dict[str, Any]:
    existing = {str(value) for value in (existing_kaspi_ids or set())}
    search = KaspiProductSearch(city_id)
    try:
        kaspi = search.search(query, sort="created-desc", limit=max_kaspi_scan, mode="brand")
    finally:
        search.close()
    crm_new = [row for row in kaspi.get("products") or [] if str(row.get("master_sku")) not in existing]
    merchant_results = (
        merchant_catalog.check_many([str(row.get("master_sku") or "") for row in crm_new], workers=6)
        if merchant_catalog is not None and crm_new
        else {}
    )
    eligible = [
        row for row in crm_new
        if not merchant_results.get(str(row.get("master_sku") or ""), {}).get("exists")
        and not merchant_results.get(str(row.get("master_sku") or ""), {}).get("error")
    ]

    profile = OzonSessionResolver().resolve()
    client = OzonSessionHttpClient(profile)
    verifier = ImageVerifier() if image_verify else None
    confirmed_rows: list[dict[str, Any]] = []
    review_rows: list[dict[str, Any]] = []
    lookup_errors: list[dict[str, str]] = []
    requested = max(1, int(target_new))
    # Do not stop after the first N Kaspi cards: the first rows can legitimately
    # have no exact Ozon twin. Keep scanning until N complete visual pairs are
    # found, with a bounded budget so one discovery job cannot hammer Ozon.
    match_attempt_limit = min(len(eligible), max(30, requested * 5))
    matched_products_checked = 0
    blocked_in_a_row = 0
    try:
        for product in eligible[:match_attempt_limit]:
            matched_products_checked += 1
            try:
                ozon = _best_ozon_match(client, product, max_queries=max_ozon_queries, verifier=verifier)
            except Exception as exc:
                lookup_errors.append({
                    "kaspi_product_id": str(product.get("master_sku") or ""),
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                })
                continue
            best = ozon.get("best") or {}
            attempts = list(ozon.get("queries") or [])
            if attempts and all(bool(attempt.get("blocked")) for attempt in attempts):
                blocked_in_a_row += 1
            else:
                blocked_in_a_row = 0
            row = {
                "kaspi_product_id": str(product.get("master_sku") or ""),
                "merchant_sku": str(product.get("master_sku") or ""),
                "product_name": product.get("title"),
                "brand": product.get("brand"),
                "image_url": product.get("image_url"),
                "product_url": product.get("kaspi_url"),
                "page_visible_price_kzt": product.get("price_kzt"),
                "supplier_url": best.get("supplier_url") or best.get("ozon_url"),
                "supplier_price_kzt": best.get("supplier_price_kzt"),
                "supplier_delivery_days": best.get("supplier_delivery_days"),
                "supplier_delivery_text": best.get("supplier_delivery_text") or best.get("delivery_text"),
                "supplier_delivery_date": best.get("supplier_delivery_date") or best.get("delivery_date"),
                "supplier_offer_sku": best.get("supplier_offer_sku") or best.get("sku"),
                "supplier_seller_name": best.get("supplier_seller_name"),
                "supplier_seller_rating": best.get("supplier_seller_rating"),
                "supplier_seller_reviews": best.get("supplier_seller_reviews"),
                "supplier_product_title": best.get("title"),
                "supplier_image_url": best.get("image_url"),
                "supplier_image_urls": _image_urls(best),
                "supplier_rating": best.get("rating"),
                "supplier_reviews": best.get("reviews"),
                "supplier_offer_count": best.get("supplier_offer_count"),
                "supplier_price_source": best.get("supplier_price_source"),
                "selection_reason": best.get("selection_reason"),
                "match_status": best.get("match_status") or "NO_RESULT",
                "match_score": best.get("match_score"),
                "match_reasons": best.get("match_reasons") or [],
                "image_match": best.get("image_match") or {},
                "queries_tested": best.get("queries_tested") or len(ozon.get("queries") or []),
                "strict_candidates_checked": best.get("strict_candidates_checked") or 0,
                "priced_strict_candidates": best.get("priced_strict_candidates") or 0,
                "total_supplier_offers_checked": best.get("total_supplier_offers_checked") or 0,
                "offers": {"kaspi": product, "ozon": ozon},
            }
            complete_pair = bool(
                row["match_status"] == "CONFIRMED"
                and row["supplier_url"]
                and isinstance(row["supplier_price_kzt"], int)
                and row["supplier_price_kzt"] > 0
                and row["supplier_image_url"]
            )
            if complete_pair:
                confirmed_rows.append(row)
            else:
                review_rows.append(row)
            if len(confirmed_rows) >= requested or blocked_in_a_row >= 2:
                break
    finally:
        client.close()
        if verifier is not None:
            verifier.close()
    rows = (confirmed_rows + review_rows)[:requested]
    return {
        "mode": "full",
        "query": query,
        "rows": rows,
        "scanned": len(kaspi.get("products") or []),
        "excluded_existing_crm": len(existing),
        "excluded_existing_merchant": sum(bool(value.get("exists")) for value in merchant_results.values()),
        "merchant_membership_errors": sum(bool(value.get("error")) for value in merchant_results.values()),
        "eligible_new": len(eligible),
        "match_attempt_limit": match_attempt_limit,
        "matched_products_checked": matched_products_checked,
        "confirmed_pairs": len(confirmed_rows),
        "manual_review_pairs": len(review_rows),
        "lookup_errors": lookup_errors[:12],
        "elapsed_ms": (kaspi.get("stats") or {}).get("elapsed_ms"),
        "browser_used": False,
    }


def discover_popular_products(
    *,
    query: str,
    city_id: str,
    zone_id: str,
    target_new: int,
    max_kaspi_scan: int,
    minimum_reviews: int = 50,
    maximum_sellers: int = 5,
    existing_kaspi_ids: set[str] | None = None,
    merchant_catalog: "MerchantOfferApi | None" = None,
    seller_count_resolver: SellerCountResolver | None = None,
) -> dict[str, Any]:
    """Find proven Kaspi demand without attempting any automatic Ozon match."""

    requested = max(1, int(target_new))
    minimum_reviews = max(0, int(minimum_reviews))
    maximum_sellers = max(1, int(maximum_sellers))
    existing = {str(value) for value in (existing_kaspi_ids or set())}
    search = KaspiProductSearch(city_id)
    try:
        kaspi = search.search(query, sort="rating", limit=max_kaspi_scan, mode="text")
    finally:
        search.close()

    scanned = list(kaspi.get("products") or [])
    crm_new = [row for row in scanned if str(row.get("master_sku")) not in existing]
    reviewed = [row for row in crm_new if (_count(row.get("reviews")) or 0) >= minimum_reviews]
    reviewed.sort(
        key=lambda row: (
            -(_count(row.get("reviews")) or 0),
            -_rating(row.get("rating")),
            _count(row.get("seller_count")) or 10**9,
            str(row.get("title") or "").casefold(),
        )
    )
    merchant_results = (
        merchant_catalog.check_many(
            [str(row.get("master_sku") or "") for row in reviewed],
            workers=6,
        )
        if merchant_catalog is not None and reviewed
        else {}
    )
    eligible = [
        row
        for row in reviewed
        if not merchant_results.get(str(row.get("master_sku") or ""), {}).get("exists")
        and not merchant_results.get(str(row.get("master_sku") or ""), {}).get("error")
    ]

    resolver = seller_count_resolver or _resolve_kaspi_seller_count
    rows: list[dict[str, Any]] = []
    lookup_errors: list[dict[str, str]] = []
    sellers_checked = 0
    excluded_too_many_sellers = 0
    excluded_unknown_sellers = 0
    for product in eligible:
        sellers_checked += 1
        try:
            seller_count, seller_details = resolver(
                product,
                city_id,
                zone_id,
                maximum_sellers,
            )
        except Exception as exc:
            seller_count = None
            seller_details = {"seller_count_source": "error"}
            lookup_errors.append({
                "kaspi_product_id": str(product.get("master_sku") or ""),
                "error": f"{type(exc).__name__}: {exc}"[:500],
            })
        if seller_count is None:
            excluded_unknown_sellers += 1
            continue
        if seller_count > maximum_sellers:
            excluded_too_many_sellers += 1
            continue

        kaspi_details = {
            **product,
            "seller_count": seller_count,
            **seller_details,
        }
        rows.append({
            "kaspi_product_id": str(product.get("master_sku") or ""),
            "merchant_sku": str(product.get("master_sku") or ""),
            "product_name": product.get("title"),
            "brand": product.get("brand"),
            "image_url": product.get("image_url"),
            "product_url": product.get("kaspi_url"),
            "page_visible_price_kzt": product.get("price_kzt"),
            "supplier_url": None,
            "supplier_price_kzt": None,
            "match_status": "NO_RESULT",
            "match_score": None,
            "offers": {
                "kaspi": kaspi_details,
                "discovery": {
                    "mode": "popular",
                    "minimum_reviews": minimum_reviews,
                    "maximum_sellers": maximum_sellers,
                    "reviews": _count(product.get("reviews")) or 0,
                    "rating": _rating(product.get("rating")),
                    "seller_count": seller_count,
                },
            },
        })
        if len(rows) >= requested:
            break

    return {
        "mode": "popular",
        "query": query,
        "rows": rows,
        "scanned": len(scanned),
        "excluded_existing_crm": len(existing),
        "excluded_below_min_reviews": len(crm_new) - len(reviewed),
        "excluded_existing_merchant": sum(
            bool(value.get("exists")) for value in merchant_results.values()
        ),
        "merchant_membership_errors": sum(
            bool(value.get("error")) for value in merchant_results.values()
        ),
        "eligible_new": len(eligible),
        "minimum_reviews": minimum_reviews,
        "maximum_sellers": maximum_sellers,
        "seller_counts_checked": sellers_checked,
        "excluded_too_many_sellers": excluded_too_many_sellers,
        "excluded_unknown_sellers": excluded_unknown_sellers,
        "matched_products_checked": 0,
        "confirmed_pairs": 0,
        "manual_review_pairs": len(rows),
        "lookup_errors": lookup_errors[:12],
        "elapsed_ms": (kaspi.get("stats") or {}).get("elapsed_ms"),
        "browser_used": False,
    }


def validate_supplier_url(url: str, *, product: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate an operator-selected Ozon card without changing its identity.

    A pasted URL is an explicit operator decision.  Its displayed product-page
    price and delivery are authoritative; search results and the other-sellers
    modal must never replace it with another card or offer.
    """

    profile = OzonSessionResolver().resolve()
    client = OzonSessionHttpClient(profile)
    product_id = _product_id_from_ozon_url(url)
    try:
        page_detail = client.product_page_price(url, product_id)
    finally:
        client.close()

    price = page_detail.get("price_kzt")
    if not isinstance(price, int) or isinstance(price, bool) or price <= 0:
        raise RuntimeError("По ссылке Ozon не найдена подтверждённая цена в KZT")
    card = page_detail.get("card") if isinstance(page_detail.get("card"), dict) else {}
    exact_product_id = str(page_detail.get("product_id") or product_id or "").strip() or None
    return {
        "supplier_url": url,
        "supplier_price_kzt": price,
        "supplier_price_source": f"manual_product_page.{page_detail.get('price_source') or 'webPrice'}",
        "supplier_delivery_days": page_detail.get("delivery_days"),
        "supplier_delivery_text": page_detail.get("delivery_text"),
        "supplier_delivery_date": page_detail.get("delivery_date"),
        "supplier_offer_sku": exact_product_id,
        "supplier_seller_name": "Ozon",
        "supplier_seller_rating": None,
        "supplier_seller_reviews": None,
        "supplier_offer_count": 1,
        "supplier_product_title": card.get("title"),
        "supplier_image_url": card.get("image_url"),
        "supplier_image_urls": list(card.get("image_urls") or [])[:6],
        "supplier_rating": card.get("rating"),
        "supplier_reviews": card.get("reviews"),
        "match_status": "OPERATOR_CONFIRMED",
        "match_score": 1.0,
        "match_reasons": ["operator_selected_exact_url"],
        "image_match": {"status": "OPERATOR_CONFIRMED"},
        "search_attempts": [],
        "price_hint_error": None,
        "manual_override": True,
        "visual_review_required": False,
        "price_confirmed": True,
        "validated": True,
    }
