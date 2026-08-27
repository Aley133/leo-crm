from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tools.ozon_http.image_verify import ImageVerifier
from tools.ozon_http.matcher import build_search_queries, rank_product
from tools.ozon_http.resolver import OzonSessionResolver
from tools.ozon_http.session_client import OzonSessionHttpClient

from .kaspi_search import KaspiProductSearch

if TYPE_CHECKING:
    from .kaspi_offer_creator import MerchantOfferApi


def _image_urls(row: dict[str, Any]) -> list[str]:
    values = list(row.get("image_urls") or [])
    if row.get("image_url") and row["image_url"] not in values:
        values.insert(0, row["image_url"])
    return [str(value) for value in values if str(value).startswith("http")]


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
        ranked = rank_product(product, list(merged.values()))
        if ranked and ranked[0].get("match_status") == "CONFIRMED" and float(ranked[0].get("match_score") or 0) >= 0.94:
            break
        if attempt.get("blocked"):
            break

    ranked = rank_product(product, list(merged.values()))
    if verifier is not None:
        for candidate in ranked[:5]:
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
        ranked.sort(key=lambda x: (x.get("match_status") == "CONFIRMED", x.get("match_score") or 0), reverse=True)

    best = dict(ranked[0]) if ranked else None
    if best and best.get("match_status") == "CONFIRMED" and best.get("ozon_url"):
        detail = client.product_price_hints(str(best["ozon_url"]))
        offer = detail.get("cheaper_offer") or {}
        if detail.get("cheaper_price_kzt"):
            best.update({
                "supplier_price_kzt": detail["cheaper_price_kzt"],
                "supplier_url": offer.get("product_url") or best.get("ozon_url"),
                "supplier_offer_sku": offer.get("offer_sku") or best.get("sku"),
                "supplier_seller_name": offer.get("seller_name"),
                "supplier_delivery_days": offer.get("delivery_days"),
                "supplier_offer_count": detail.get("other_offer_count") or 0,
            })
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
    selected = [
        row for row in crm_new
        if not merchant_results.get(str(row.get("master_sku") or ""), {}).get("exists")
        and not merchant_results.get(str(row.get("master_sku") or ""), {}).get("error")
    ][:target_new]

    profile = OzonSessionResolver().resolve()
    client = OzonSessionHttpClient(profile)
    verifier = ImageVerifier() if image_verify else None
    rows: list[dict[str, Any]] = []
    try:
        for product in selected:
            ozon = _best_ozon_match(client, product, max_queries=max_ozon_queries, verifier=verifier)
            best = ozon.get("best") or {}
            rows.append({
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
                "supplier_offer_sku": best.get("supplier_offer_sku") or best.get("sku"),
                "supplier_seller_name": best.get("supplier_seller_name"),
                "match_status": best.get("match_status") or "NO_RESULT",
                "match_score": best.get("match_score"),
                "offers": {"kaspi": product, "ozon": ozon},
            })
    finally:
        client.close()
        if verifier is not None:
            verifier.close()
    return {
        "query": query,
        "rows": rows,
        "scanned": len(kaspi.get("products") or []),
        "excluded_existing_crm": len(existing),
        "excluded_existing_merchant": sum(bool(value.get("exists")) for value in merchant_results.values()),
        "merchant_membership_errors": sum(bool(value.get("error")) for value in merchant_results.values()),
        "elapsed_ms": (kaspi.get("stats") or {}).get("elapsed_ms"),
        "browser_used": False,
    }


def validate_supplier_url(url: str) -> dict[str, Any]:
    profile = OzonSessionResolver().resolve()
    client = OzonSessionHttpClient(profile)
    try:
        detail = client.product_price_hints(url)
    finally:
        client.close()
    offer = detail.get("cheaper_offer") or {}
    price = detail.get("cheaper_price_kzt")
    if not detail.get("ok") or not isinstance(price, int) or price <= 0:
        raise RuntimeError("По ссылке Ozon не найдена подтверждённая цена в KZT")
    return {
        "supplier_url": offer.get("product_url") or url,
        "supplier_price_kzt": price,
        "supplier_delivery_days": offer.get("delivery_days"),
        "supplier_offer_sku": offer.get("offer_sku") or detail.get("product_id"),
        "supplier_seller_name": offer.get("seller_name"),
        "supplier_offer_count": detail.get("other_offer_count") or 0,
        "validated": True,
    }
