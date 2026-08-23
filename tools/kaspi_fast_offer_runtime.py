from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from threading import Lock
from typing import Any

import httpx

from tools import kaspi_fast_dumping_agent as base
from tools.kaspi_fast_dumping_session import (
    IDMC_LOGIN_PAGE,
    IDMC_LOGIN_URL,
    MC_OAUTH_ENTRY_URL,
    MC_ROOT_URL,
    PROCESS_URL,
    KaspiMerchantSession,
)


BFF_OFFER_URL = "https://mc.shop.kaspi.kz/bff/offer-view/list"
_AUTH_CLIENTS: dict[int, httpx.Client] = {}
_AUTH_LOCK = Lock()


@dataclass(slots=True)
class OfferState:
    found: bool
    sku: str
    store_id: str | None
    stock_count: int | None
    preorder_days: int | None
    nested_available: str | None
    row_available: bool | None
    price_kzt: int | None
    query_mode: str | None
    operation_type: str | None
    processed: bool | None
    applied_before: str | None
    raw_status: str | None

    @property
    def pending(self) -> bool:
        return str(self.operation_type or "").upper() == "IN_PROGRESS" or self.processed is False


def _headers(session: KaspiMerchantSession) -> dict[str, str]:
    return {
        "User-Agent": session.user_agent,
        "Accept-Language": "ru,en-US;q=0.9,en;q=0.8,kk;q=0.7",
        "Accept": "application/json, text/plain, */*",
        "X-Auth-Version": "3",
        "Origin": "https://kaspi.kz",
        "Referer": "https://kaspi.kz/",
    }


def _new_full_client(session: KaspiMerchantSession) -> httpx.Client:
    client = httpx.Client(
        headers={
            "User-Agent": session.user_agent,
            "Accept-Language": "ru,en-US;q=0.9,en;q=0.8,kk;q=0.7",
        },
        follow_redirects=True,
        timeout=session.timeout_seconds,
    )
    client.get(MC_ROOT_URL, follow_redirects=False)
    client.get(MC_OAUTH_ENTRY_URL, follow_redirects=False)
    client.get(IDMC_LOGIN_PAGE)
    login = client.post(
        IDMC_LOGIN_URL,
        json={"_u": session.email, "_p": session.password, "_r_d": False},
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://idmc.shop.kaspi.kz",
            "Referer": "https://idmc.shop.kaspi.kz/login",
        },
        follow_redirects=False,
    )
    login.raise_for_status()
    client.get(MC_OAUTH_ENTRY_URL).raise_for_status()
    return client


def _client(session: KaspiMerchantSession, *, force_refresh: bool = False) -> httpx.Client:
    key = id(session)
    with _AUTH_LOCK:
        if force_refresh:
            old = _AUTH_CLIENTS.pop(key, None)
            if old is not None:
                old.close()
        cached = _AUTH_CLIENTS.get(key)
        if cached is None:
            cached = _new_full_client(session)
            _AUTH_CLIENTS[key] = cached
        return cached


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


def _parse_offer(
    *,
    payload: Any,
    sku: str,
    store_id: str,
    city_id: str,
    query_mode: str,
) -> OfferState | None:
    exact = [row for row in _rows(payload) if str(row.get("sku") or "").strip() == sku]
    if not exact:
        return None
    for row in exact:
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
        if availability is None:
            continue
        stock = availability.get("stockCount")
        preorder = availability.get("preOrder")
        price = None
        city_prices = row.get("cityPrices")
        if isinstance(city_prices, list):
            city_price = next(
                (
                    item
                    for item in city_prices
                    if isinstance(item, dict)
                    and str(item.get("cityId") or "").strip() == city_id
                ),
                None,
            )
            if city_price is not None:
                price = city_price.get("value")
        if price in (None, ""):
            price = row.get("minPrice") or row.get("maxPrice")
        return OfferState(
            found=True,
            sku=sku,
            store_id=store_id,
            stock_count=None if stock in (None, "") else int(float(stock)),
            preorder_days=None if preorder in (None, "") else int(float(preorder)),
            nested_available=(
                None
                if availability.get("available") is None
                else str(availability.get("available")).strip().lower()
            ),
            row_available=(None if row.get("available") is None else bool(row.get("available"))),
            price_kzt=None if price in (None, "") else int(Decimal(str(price))),
            query_mode=query_mode,
            operation_type=(None if row.get("operationType") is None else str(row.get("operationType"))),
            processed=(None if row.get("processed") is None else bool(row.get("processed"))),
            applied_before=(
                None
                if row.get("appliedBeforeDateTime") is None
                else str(row.get("appliedBeforeDateTime"))
            ),
            raw_status=(None if row.get("status") is None else str(row.get("status"))),
        )
    return None


def read_offer_state(
    session: KaspiMerchantSession,
    *,
    merchant_uid: str,
    sku: str,
    store_id: str,
    city_id: str,
) -> OfferState:
    last_error: Exception | None = None
    for refresh in (False, True):
        client = _client(session, force_refresh=refresh)
        try:
            for mode, active in (("active", True), ("inactive", False), ("all", None)):
                params: dict[str, Any] = {
                    "m": merchant_uid,
                    "p": 0,
                    "l": 10,
                    "t": sku,
                }
                if active is not None:
                    params["a"] = str(active).lower()
                response = client.get(BFF_OFFER_URL, params=params, headers=_headers(session))
                if response.status_code in (401, 403):
                    raise PermissionError(f"Merchant BFF returned HTTP {response.status_code}")
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise RuntimeError("Merchant BFF returned non-JSON response") from exc
                state = _parse_offer(
                    payload=payload,
                    sku=sku,
                    store_id=store_id,
                    city_id=city_id,
                    query_mode=mode,
                )
                if state is not None:
                    return state
            return OfferState(
                found=False,
                sku=sku,
                store_id=None,
                stock_count=None,
                preorder_days=None,
                nested_available=None,
                row_available=None,
                price_kzt=None,
                query_mode=None,
                operation_type=None,
                processed=None,
                applied_before=None,
                raw_status=None,
            )
        except Exception as exc:
            last_error = exc
            if refresh:
                break
    raise RuntimeError(f"Не удалось прочитать Merchant offer-state: {last_error}")


def write_offer_state(
    session: KaspiMerchantSession,
    *,
    merchant_uid: str,
    store_id: str,
    city_id: str,
    sku: str,
    model: str,
    stock_count: int,
    preorder_days: int,
    price: int,
) -> dict[str, Any]:
    payload = {
        "merchantUid": merchant_uid,
        "availabilities": [
            {
                "available": "yes",
                "storeId": store_id,
                "stockCount": int(stock_count),
                "preOrder": int(preorder_days),
            }
        ],
        "cityPrices": [{"cityId": city_id, "value": int(price)}],
        "sku": sku,
        "model": model,
    }
    started = time.perf_counter()
    for refresh in (False, True):
        client = _client(session, force_refresh=refresh)
        try:
            response = client.post(
                PROCESS_URL,
                json=payload,
                headers={**_headers(session), "Content-Type": "application/json"},
            )
            if response.status_code in (401, 403) and not refresh:
                continue
            try:
                body = response.json()
            except ValueError:
                body = None
            return {
                "accepted": response.is_success,
                "status_code": response.status_code,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                "operation_id": body.get("id") if isinstance(body, dict) else None,
                "error_message": None if response.is_success else f"Kaspi Merchant returned HTTP {response.status_code}",
            }
        except httpx.HTTPError as exc:
            if refresh:
                return {
                    "accepted": False,
                    "status_code": None,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                    "operation_id": None,
                    "error_message": f"{type(exc).__name__}: {exc}",
                }
    return {"accepted": False, "status_code": None, "operation_id": None, "error_message": "Kaspi write failed"}


def _state_summary(state: OfferState) -> dict[str, Any]:
    return {
        "found": state.found,
        "stock": state.stock_count,
        "preorder": state.preorder_days,
        "available": state.nested_available,
        "row_available": state.row_available,
        "price": state.price_kzt,
        "query_mode": state.query_mode,
        "operation_type": state.operation_type,
        "processed": state.processed,
        "applied_before": state.applied_before,
    }


def _matches(
    state: OfferState,
    *,
    mode: str,
    stock: int,
    preorder: int,
    price: int | None,
) -> bool:
    if not state.found:
        return False
    if state.stock_count != int(stock) or state.preorder_days != int(preorder):
        return False
    if price is not None and state.price_kzt != int(price):
        return False
    if mode == "off":
        return state.stock_count == 0 and state.preorder_days == 0
    return state.nested_available == "yes" and state.row_available is not False


async def process_apply(
    *,
    api_url: str,
    token: str,
    job: dict,
    agent_id: str,
    workspace_id: int,
    merchant_uid: str,
    store_id: str,
    merchant_session: KaspiMerchantSession,
) -> None:
    identity = {
        "agent_id": agent_id,
        "workspace_id": workspace_id,
        "lease_token": job["lease_token"],
    }
    prepared = await base._post_json_with_retry(
        f"{api_url}/api/fast-dumping-agent/jobs/{job['id']}/prepare-apply",
        token,
        identity,
        operation=f"Проверка realtime offer-state #{job['id']}",
    )
    if not prepared.get("ready"):
        base._log(
            f"Offer-state #{job['id']} отменён CRM: {prepared.get('reason')}",
            workspace_id=workspace_id,
        )
        return

    sku = str(prepared["sku"])
    city_id = str(prepared["city_id"])
    mode = str(prepared.get("fulfillment_mode") or "inventory")
    crm_stock = int(prepared.get("stock_count") or 0)
    preorder = int(prepared.get("preorder_days") or 0)
    refreshed = False
    write_started = time.monotonic()
    write_result: dict[str, Any] = {}
    error_code: str | None = None
    error_message: str | None = None
    observed_price: Decimal | None = None
    verified = False
    accepted = False

    try:
        async with base._WRITE_LOCK:
            await asyncio.to_thread(merchant_session.ensure_valid_sid)
            live = await asyncio.to_thread(
                read_offer_state,
                merchant_session,
                merchant_uid=merchant_uid,
                sku=sku,
                store_id=store_id,
                city_id=city_id,
            )
            if not live.found:
                raise RuntimeError("Merchant BFF не вернул точный SKU/store")

            target_price_raw = prepared.get("target_price_kzt")
            target_price = (
                int(Decimal(str(target_price_raw)))
                if target_price_raw not in (None, "")
                else live.price_kzt
            )
            if target_price is None:
                raise RuntimeError("Не удалось определить текущую/целевую цену Kaspi")

            if mode == "inventory":
                if live.stock_count is None:
                    error_code = "kaspi_offer_read_failed"
                    error_message = "Kaspi не вернул stockCount для точного SKU/store. Write заблокирован."
                elif live.stock_count < crm_stock:
                    error_code = "kaspi_zero_vs_crm_stock" if live.stock_count == 0 else "kaspi_stock_lower_than_crm"
                    error_message = (
                        f"Защитная блокировка: CRM FIFO={crm_stock}, Kaspi stock={live.stock_count}. "
                        "Fast Agent не увеличивает остаток поверх более низкого Kaspi, чтобы не воскресить проданную единицу."
                    )
            if error_code is None and live.pending and not _matches(
                live,
                mode=mode,
                stock=crm_stock,
                preorder=preorder,
                price=target_price,
            ):
                accepted = True
                error_code = "waiting_existing_operation"
                error_message = json.dumps(_state_summary(live), ensure_ascii=False)
            elif error_code is None and _matches(
                live,
                mode=mode,
                stock=crm_stock,
                preorder=preorder,
                price=target_price,
            ):
                accepted = True
                verified = True
                observed_price = Decimal(target_price)
            elif error_code is None:
                write_result = await asyncio.to_thread(
                    write_offer_state,
                    merchant_session,
                    merchant_uid=merchant_uid,
                    store_id=store_id,
                    city_id=city_id,
                    sku=sku,
                    model=str(prepared["model"]),
                    stock_count=crm_stock,
                    preorder_days=preorder,
                    price=target_price,
                )
                accepted = bool(write_result.get("accepted"))
                if accepted:
                    after = await asyncio.to_thread(
                        read_offer_state,
                        merchant_session,
                        merchant_uid=merchant_uid,
                        sku=sku,
                        store_id=store_id,
                        city_id=city_id,
                    )
                    verified = _matches(
                        after,
                        mode=mode,
                        stock=crm_stock,
                        preorder=preorder,
                        price=target_price,
                    )
                    if verified:
                        observed_price = Decimal(target_price)
                else:
                    error_code = "merchant_write_failed"
                    error_message = str(write_result.get("error_message") or "Kaspi отклонил realtime write")

        payload = {
            **identity,
            "accepted": accepted,
            "verified": verified,
            "status_code": write_result.get("status_code") if write_result else (200 if accepted else None),
            "operation_id": write_result.get("operation_id") if write_result else None,
            "latency_seconds": round(time.monotonic() - write_started, 1),
            "observed_own_price_kzt": None if observed_price is None else format(observed_price, "f"),
            "session_refreshed": refreshed,
            "error_code": error_code,
            "error_message": error_message,
        }
    except Exception as exc:
        payload = {
            **identity,
            "accepted": False,
            "verified": False,
            "session_refreshed": refreshed,
            "error_code": "kaspi_offer_read_failed" if "BFF" in str(exc) else type(exc).__name__,
            "error_message": str(exc)[:2000],
        }

    result = await base._post_json_with_retry(
        f"{api_url}/api/fast-dumping-agent/jobs/{job['id']}/apply-complete",
        token,
        payload,
        operation=f"Подтверждение realtime offer-state #{job['id']}",
    )
    base._log(
        f"Offer-state #{job['id']}: {result.get('status')}",
        workspace_id=workspace_id,
    )


async def process_verify(
    *,
    api_url: str,
    token: str,
    job: dict,
    agent_id: str,
    workspace_id: int,
    merchant_uid: str,
    merchant_session: KaspiMerchantSession,
    store_id: str,
) -> None:
    sku = str(job.get("merchant_sku") or "")
    city_id = str(job["city_id"])
    mode = str(job.get("fulfillment_mode") or "inventory")
    stock = int(job.get("target_stock_count") or 0)
    preorder = int(job.get("target_preorder_days") or 0)
    target_raw = job.get("target_price_kzt")
    target = None if target_raw in (None, "") else int(Decimal(str(target_raw)))
    error_code = None
    error_message = None
    observed = None
    status = "succeeded"
    try:
        await asyncio.to_thread(merchant_session.ensure_valid_sid)
        live = await asyncio.to_thread(
            read_offer_state,
            merchant_session,
            merchant_uid=merchant_uid,
            sku=sku,
            store_id=store_id,
            city_id=city_id,
        )
        if _matches(live, mode=mode, stock=stock, preorder=preorder, price=target):
            observed = live.price_kzt if live.price_kzt is not None else target
        elif live.pending:
            status = "failed"
            error_code = "kaspi_operation_in_progress"
            error_message = json.dumps(_state_summary(live), ensure_ascii=False)
        else:
            status = "failed"
            error_code = "offer_state_mismatch"
            error_message = (
                f"Ожидалось mode={mode}, stock={stock}, preOrder={preorder}, price={target}; "
                f"Kaspi={json.dumps(_state_summary(live), ensure_ascii=False)}"
            )
    except Exception as exc:
        status = "failed"
        error_code = "kaspi_offer_read_failed"
        error_message = str(exc)[:2000]
        base._log(f"Offer verify #{job['id']}: {exc}", workspace_id=workspace_id)

    await base._post_json_with_retry(
        f"{api_url}/api/fast-dumping-agent/jobs/{job['id']}/verify-complete",
        token,
        {
            "agent_id": agent_id,
            "workspace_id": workspace_id,
            "lease_token": job["lease_token"],
            "status": status,
            "observed_own_price_kzt": None if observed is None else str(observed),
            "error_code": error_code,
            "error_message": error_message,
        },
        operation=f"Сохранение Merchant verify #{job['id']}",
    )
