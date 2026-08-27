from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

import httpx

from tools.kaspi_fast_dumping_session import KaspiMerchantSession

BFF_OFFER_URL = "https://mc.shop.kaspi.kz/bff/offer-view/list"
PROCESS_URL = "https://mc.shop.kaspi.kz/pricefeed/upload/merchant/process"
VALIDATE_URL = "https://mc.shop.kaspi.kz/offer-validation-api/merchant/offer/validate/v2"
NEW_CODE_URL = "https://mc.shop.kaspi.kz/content/pending/mc/product/{merchant_uid}/new-code"
LINK_TO_MASTER_URL = "https://mc.shop.kaspi.kz/content/pending/mc/product/link-to-master"
PROTOCOL_LIST_URL = "https://mc.shop.kaspi.kz/pricefeed/protocol/merchant/offer/list/s"


@dataclass(slots=True)
class OfferState:
    found: bool
    sku: str
    master_sku: str | None = None
    requested_reference: str | None = None
    store_id: str | None = None
    stock_count: int | None = None
    preorder_days: int | None = None
    nested_available: str | None = None
    row_available: bool | None = None
    price_kzt: int | None = None
    query_mode: str | None = None
    operation_type: str | None = None
    processed: bool | None = None
    raw_status: str | None = None

    def dict(self) -> dict[str, Any]:
        return asdict(self)


def _rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("content", "items", "offers", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    for key in ("content", "items", "offers"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(Decimal(str(value)))
    except Exception:
        return None


def _pick_matching_row(rows: list[dict[str, Any]], reference: str, store_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Match either a real merchant SKU or a Kaspi masterSku.

    Manual add-product does NOT use masterSku as merchant SKU. Kaspi generates a
    code such as `108842165_364257326`, then links it to masterSku `108842165`.
    The old lab filtered only row.sku == input and therefore reported a false
    negative even after a successful manual add.
    """
    reference = str(reference).strip()
    ordered = sorted(
        rows,
        key=lambda row: 0 if str(row.get("sku") or "").strip() == reference else 1,
    )
    for row in ordered:
        sku = str(row.get("sku") or "").strip()
        master_sku = str(row.get("masterSku") or "").strip()
        if reference not in (sku, master_sku):
            continue
        availabilities = row.get("availabilities")
        if not isinstance(availabilities, list):
            continue
        availability = next(
            (
                item
                for item in availabilities
                if isinstance(item, dict)
                and str(item.get("storeId") or "").strip() == store_id
            ),
            None,
        )
        if availability is not None:
            return row, availability
    return None


class MerchantOfferApi:
    """Proved Kaspi manual-add flow adapted to the existing Fast Agent session."""

    def __init__(
        self,
        session: KaspiMerchantSession,
        *,
        store_id: str,
        city_id: str,
    ) -> None:
        self.session = session
        self.merchant_uid = session.merchant_uid
        self.store_id = str(store_id).strip()
        self.city_id = str(city_id).strip()
        self.auth_report = {"authenticated": True}

    def close(self) -> None:
        return None

    @property
    def config(self):
        # Preserve the exact, lab-tested payload builder while feeding it the
        # settings already owned by LEO's Fast Agent.
        return self

    @staticmethod
    def assert_live_allowed(live: bool) -> None:
        if not live:
            raise RuntimeError("Live Kaspi write was not authorized")

    def _headers(self, sid: str) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Cookie": f"mc-sid={sid}",
            "X-Auth-Version": "3",
            "Origin": "https://kaspi.kz",
            "Referer": "https://kaspi.kz/",
            "User-Agent": self.session.user_agent,
        }

    def _request_json(self, method: str, url: str, *, json_body: Any = None, params: dict[str, Any] | None = None):
        sid, _ = self.session.ensure_valid_sid()
        kwargs: dict[str, Any] = {"headers": self._headers(sid)}
        if json_body is not None:
            kwargs["json"] = json_body
        if params is not None:
            kwargs["params"] = params
        response = httpx.request(method, url, timeout=self.session.timeout_seconds, **kwargs)
        if response.status_code in (401, 403):
            sid, _ = self.session.ensure_valid_sid(force_refresh=True)
            kwargs["headers"] = self._headers(sid)
            response = httpx.request(method, url, timeout=self.session.timeout_seconds, **kwargs)
        return response

    def read_offer(self, reference: str) -> OfferState:
        reference = str(reference).strip()
        for mode, active in (("active", True), ("inactive", False), ("all", None)):
            params: dict[str, Any] = {
                "m": self.config.merchant_uid,
                "p": 0,
                "l": 20,
                "t": reference,
            }
            if active is not None:
                params["a"] = str(active).lower()
            response = self._request_json("GET", BFF_OFFER_URL, params=params)
            response.raise_for_status()
            payload = response.json()
            picked = _pick_matching_row(_rows(payload), reference, self.config.store_id)
            if picked is None:
                continue
            row, availability = picked
            city_price = None
            city_prices = row.get("cityPrices")
            if isinstance(city_prices, list):
                city_price = next(
                    (
                        item.get("value")
                        for item in city_prices
                        if isinstance(item, dict)
                        and str(item.get("cityId") or "").strip() == self.config.city_id
                    ),
                    None,
                )
            if city_price in (None, ""):
                city_price = row.get("minPrice") or row.get("maxPrice")
            actual_sku = str(row.get("sku") or reference).strip()
            master_sku = str(row.get("masterSku") or "").strip() or None
            return OfferState(
                found=True,
                sku=actual_sku,
                master_sku=master_sku,
                requested_reference=reference,
                store_id=self.config.store_id,
                stock_count=_to_int(availability.get("stockCount")),
                preorder_days=_to_int(availability.get("preOrder")),
                nested_available=(
                    None
                    if availability.get("available") is None
                    else str(availability.get("available")).strip().lower()
                ),
                row_available=(None if row.get("available") is None else bool(row.get("available"))),
                price_kzt=_to_int(city_price),
                query_mode=mode,
                operation_type=(None if row.get("operationType") is None else str(row.get("operationType"))),
                processed=(None if row.get("processed") is None else bool(row.get("processed"))),
                raw_status=(None if row.get("status") is None else str(row.get("status"))),
            )
        return OfferState(found=False, sku=reference, requested_reference=reference)

    def check_many(self, master_skus: list[str], *, workers: int = 6) -> dict[str, dict[str, Any]]:
        """Read-only, bounded Merchant BFF membership check from the lab flow."""

        unique = list(dict.fromkeys(str(value).strip() for value in master_skus if str(value).strip()))
        sid, _ = self.session.ensure_valid_sid()
        headers = self._headers(sid)

        def lookup(master_sku: str) -> tuple[str, dict[str, Any]]:
            params = {"m": self.merchant_uid, "p": 0, "l": 20, "t": master_sku}
            try:
                response = httpx.get(BFF_OFFER_URL, headers=headers, params=params, timeout=self.session.timeout_seconds)
                if response.status_code == 429:
                    return master_sku, {"exists": False, "error": "HTTP 429", "status_code": 429}
                response.raise_for_status()
                rows = _rows(response.json())
                for row in rows:
                    sku = str(row.get("sku") or "").strip()
                    master = str(row.get("masterSku") or "").strip()
                    if master_sku in {sku, master}:
                        return master_sku, {"exists": True, "merchant_sku": sku or None}
                return master_sku, {"exists": False}
            except Exception as exc:
                return master_sku, {"exists": False, "error": f"{type(exc).__name__}: {exc}"}

        results: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=min(12, max(1, int(workers))), thread_name_prefix="merchant-bff") as pool:
            futures = [pool.submit(lookup, sku) for sku in unique]
            for future in as_completed(futures):
                sku, result = future.result()
                results[sku] = result
        return results

    def process_payload(
        self,
        *,
        sku: str,
        model: str,
        price: int,
        stock: int,
        preorder: int | None,
        include_brand: bool = False,
    ) -> dict[str, Any]:
        availability: dict[str, Any] = {
            "available": "yes",
            "storeId": self.config.store_id,
            "stockCount": int(stock),
        }
        if preorder is not None:
            availability["preOrder"] = int(preorder)
        payload: dict[str, Any] = {
            "cityPrices": [{"cityId": self.config.city_id, "value": int(price)}],
            "availabilities": [availability],
            "merchantUid": self.config.merchant_uid,
            "sku": str(sku).strip(),
            "model": str(model).strip(),
        }
        if include_brand:
            payload["brand"] = ""
        return payload

    def process_offer(
        self,
        *,
        sku: str,
        model: str,
        price: int,
        stock: int,
        preorder: int,
        live: bool,
    ) -> dict[str, Any]:
        payload = self.process_payload(
            sku=sku,
            model=model,
            price=price,
            stock=stock,
            preorder=preorder,
        )
        preview = {
            "method": "POST",
            "url": PROCESS_URL,
            "json": payload,
            "note": "Realtime endpoint Fast Dumping для уже существующего merchant-offer.",
        }
        if not live:
            return {"dry_run": True, "request": preview}
        self.config.assert_live_allowed(True)
        started = time.perf_counter()
        response = self._request_json("POST", PROCESS_URL, json_body=payload)
        try:
            body = response.json()
        except ValueError:
            body = {"text": response.text[:1000]}
        return {
            "dry_run": False,
            "accepted": response.is_success,
            "status_code": response.status_code,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "operation_id": body.get("id") if isinstance(body, dict) else None,
            "body": body,
        }

    def create_flow_preview(self, *, master_sku: str, model: str, price: int, stock: int, preorder: int) -> dict[str, Any]:
        generated = f"{master_sku}_<new-code>"
        return {
            "dry_run": True,
            "discovered_from_har": True,
            "steps": [
                {
                    "name": "validate_choose",
                    "method": "POST",
                    "url": VALIDATE_URL,
                    "json": {
                        "action": "LINK__TO_MASTER_CHOOSE",
                        "merchantUid": self.config.merchant_uid,
                        "offers": [{"masterSku": master_sku}],
                    },
                },
                {
                    "name": "new_code",
                    "method": "GET",
                    "url": NEW_CODE_URL.format(merchant_uid=self.config.merchant_uid),
                    "result": "<new-code>",
                },
                {
                    "name": "validate_price_stock",
                    "method": "POST",
                    "url": VALIDATE_URL,
                    "json": {
                        "action": "LINK__PRICE_SAVE",
                        "merchantUid": self.config.merchant_uid,
                        "offers": [{
                            "masterSku": master_sku,
                            "availabilities": [{
                                "available": "yes",
                                "storeId": self.config.store_id,
                                "stockLevel": int(stock),
                            }],
                        }],
                    },
                },
                {
                    "name": "link_to_master",
                    "method": "POST",
                    "url": LINK_TO_MASTER_URL,
                    "json": {
                        "merchantCode": self.config.merchant_uid,
                        "merchantProductCode": generated,
                        "masterProductCode": master_sku,
                    },
                },
                {
                    "name": "initial_process",
                    "method": "POST",
                    "url": PROCESS_URL,
                    "json": self.process_payload(
                        sku=generated,
                        model=model,
                        price=price,
                        stock=stock,
                        preorder=None,
                        include_brand=True,
                    ),
                    "note": "Повторяет ручной Merchant Cabinet: первый process без preOrder.",
                },
                {
                    "name": "protocol_verify",
                    "method": "POST",
                    "url": PROTOCOL_LIST_URL,
                    "json": {"merchantUid": self.config.merchant_uid, "skuList": [generated]},
                },
                {
                    "name": "set_preorder",
                    "method": "POST",
                    "url": PROCESS_URL,
                    "json": self.process_payload(
                        sku=generated,
                        model=model,
                        price=price,
                        stock=stock,
                        preorder=preorder,
                    ),
                    "when": f"после создания оффера; preOrder={int(preorder)}",
                },
            ],
        }

    @staticmethod
    def _validation_ok(response) -> tuple[bool, Any]:
        try:
            body = response.json()
        except ValueError:
            return False, {"text": response.text[:1000]}
        return bool(response.is_success and isinstance(body, dict) and body.get("valid") is True), body

    def _validate(self, payload: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        response = self._request_json("POST", VALIDATE_URL, json_body=payload)
        valid, body = self._validation_ok(response)
        return {
            "ok": valid,
            "status_code": response.status_code,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "body": body,
        }

    def _new_code(self) -> dict[str, Any]:
        started = time.perf_counter()
        response = self._request_json(
            "GET", NEW_CODE_URL.format(merchant_uid=self.config.merchant_uid)
        )
        text = response.text.strip().strip('"')
        ok = bool(response.is_success and text and text.isdigit())
        return {
            "ok": ok,
            "status_code": response.status_code,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "code": text if ok else None,
            "raw": None if ok else response.text[:1000],
        }

    def _link_to_master(self, *, master_sku: str, merchant_sku: str) -> dict[str, Any]:
        payload = {
            "merchantCode": self.config.merchant_uid,
            "merchantProductCode": merchant_sku,
            "masterProductCode": master_sku,
        }
        started = time.perf_counter()
        response = self._request_json("POST", LINK_TO_MASTER_URL, json_body=payload)
        return {
            "ok": response.is_success,
            "status_code": response.status_code,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "request": payload,
            "body": response.text[:1000],
        }

    def _initial_manual_process(self, *, merchant_sku: str, model: str, price: int, stock: int) -> dict[str, Any]:
        payload = self.process_payload(
            sku=merchant_sku,
            model=model,
            price=price,
            stock=stock,
            preorder=None,
            include_brand=True,
        )
        started = time.perf_counter()
        response = self._request_json("POST", PROCESS_URL, json_body=payload)
        try:
            body = response.json()
        except ValueError:
            body = {"text": response.text[:1000]}
        return {
            "ok": response.is_success,
            "status_code": response.status_code,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "operation_id": body.get("id") if isinstance(body, dict) else None,
            "request": payload,
            "body": body,
        }

    def _protocol_offer(self, merchant_sku: str) -> dict[str, Any]:
        payload = {"merchantUid": self.config.merchant_uid, "skuList": [merchant_sku]}
        started = time.perf_counter()
        response = self._request_json("POST", PROTOCOL_LIST_URL, json_body=payload)
        try:
            body = response.json()
        except ValueError:
            body = {"text": response.text[:1000]}
        rows = body if isinstance(body, list) else []
        matched = any(str(row.get("sku") or "").strip() == merchant_sku for row in rows if isinstance(row, dict))
        return {
            "ok": response.is_success,
            "found": matched,
            "status_code": response.status_code,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "body": body,
        }

    def create_linked_offer(
        self,
        *,
        master_sku: str,
        model: str,
        price: int,
        stock: int,
        preorder: int,
        live: bool,
        attempts: int = 12,
        poll_seconds: float = 2.0,
    ) -> dict[str, Any]:
        master_sku = str(master_sku).strip()
        before = self.read_offer(master_sku)
        if before.found:
            return {
                "result": "ALREADY_EXISTS",
                "before": before.dict(),
                "merchant_sku": before.sku,
                "message": "Оффер уже привязан. Для изменений используй обычный realtime process.",
            }
        if not live:
            return {
                "result": "DRY_RUN_CREATE_FLOW",
                "before": before.dict(),
                **self.create_flow_preview(
                    master_sku=master_sku,
                    model=model,
                    price=price,
                    stock=stock,
                    preorder=preorder,
                ),
            }

        self.config.assert_live_allowed(True)
        steps: list[dict[str, Any]] = []

        choose_payload = {
            "action": "LINK__TO_MASTER_CHOOSE",
            "merchantUid": self.config.merchant_uid,
            "offers": [{"masterSku": master_sku}],
        }
        choose = self._validate(choose_payload)
        steps.append({"name": "validate_choose", **choose})
        if not choose["ok"]:
            return {"result": "VALIDATE_CHOOSE_REJECTED", "before": before.dict(), "steps": steps}

        code = self._new_code()
        steps.append({"name": "new_code", **code})
        if not code["ok"]:
            return {"result": "NEW_CODE_FAILED", "before": before.dict(), "steps": steps}
        merchant_sku = f"{master_sku}_{code['code']}"

        price_payload = {
            "action": "LINK__PRICE_SAVE",
            "merchantUid": self.config.merchant_uid,
            "offers": [{
                "masterSku": master_sku,
                "availabilities": [{
                    "available": "yes",
                    "storeId": self.config.store_id,
                    "stockLevel": int(stock),
                }],
            }],
        }
        price_validation = self._validate(price_payload)
        steps.append({"name": "validate_price_stock", **price_validation})
        if not price_validation["ok"]:
            return {
                "result": "VALIDATE_PRICE_STOCK_REJECTED",
                "before": before.dict(),
                "merchant_sku": merchant_sku,
                "steps": steps,
            }

        linked = self._link_to_master(master_sku=master_sku, merchant_sku=merchant_sku)
        steps.append({"name": "link_to_master", **linked})
        if not linked["ok"]:
            return {
                "result": "LINK_TO_MASTER_FAILED",
                "before": before.dict(),
                "merchant_sku": merchant_sku,
                "steps": steps,
            }

        initial = self._initial_manual_process(
            merchant_sku=merchant_sku,
            model=model,
            price=price,
            stock=stock,
        )
        steps.append({"name": "initial_process", **initial})
        if not initial["ok"]:
            return {
                "result": "INITIAL_PROCESS_FAILED",
                "before": before.dict(),
                "merchant_sku": merchant_sku,
                "steps": steps,
            }

        protocol = self._protocol_offer(merchant_sku)
        steps.append({"name": "protocol_verify", **protocol})

        history: list[dict[str, Any]] = []
        visible: OfferState | None = None
        for attempt in range(1, max(1, attempts) + 1):
            state = self.read_offer(merchant_sku)
            history.append({"attempt": attempt, "state": state.dict()})
            if state.found:
                visible = state
                break
            if attempt < attempts:
                time.sleep(max(0.5, poll_seconds))

        if visible is None:
            return {
                "result": "LINKED_PROCESS_ACCEPTED_NOT_VISIBLE",
                "before": before.dict(),
                "merchant_sku": merchant_sku,
                "steps": steps,
                "history": history,
                "note": "link-to-master и process приняты, но BFF ещё не увидел merchant SKU.",
            }

        preorder_write = None
        if int(preorder) != int(visible.preorder_days or 0):
            preorder_write = self.process_offer(
                sku=merchant_sku,
                model=model,
                price=price,
                stock=stock,
                preorder=preorder,
                live=True,
            )
            steps.append({"name": "set_preorder", **preorder_write})
            if preorder_write.get("accepted"):
                for attempt in range(1, 8):
                    state = self.read_offer(merchant_sku)
                    history.append({"attempt": f"preorder-{attempt}", "state": state.dict()})
                    if (
                        state.found
                        and state.price_kzt == int(price)
                        and state.stock_count == int(stock)
                        and state.preorder_days == int(preorder)
                    ):
                        visible = state
                        break
                    if attempt < 7:
                        time.sleep(2)

        final = self.read_offer(merchant_sku)
        return {
            "result": "CREATED_AND_VISIBLE",
            "before": before.dict(),
            "merchant_sku": merchant_sku,
            "master_sku": master_sku,
            "steps": steps,
            "history": history,
            "after": final.dict(),
            "preorder_requested": int(preorder),
            "preorder_verified": final.preorder_days == int(preorder),
        }
