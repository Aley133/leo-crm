from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from backend.app.kaspi_seller.graphql import (
    GET_ORDER_DETAILS_OPERATION,
    GET_ORDER_DETAILS_QUERY,
    GET_ORDER_STATE_OPERATION,
    GET_ORDER_STATE_QUERY,
)
from backend.app.kaspi_seller.mapper import map_seller_order_snapshot
from backend.app.supplier_adapters.playwright_pool import PlaywrightBrowserPool


class KaspiSellerBrowserError(RuntimeError):
    """Base error for verified Kaspi Seller browser access."""


class KaspiSellerNotAuthorized(KaspiSellerBrowserError):
    pass


class KaspiSellerGraphQLError(KaspiSellerBrowserError):
    pass


class KaspiSellerOrderNotFound(KaspiSellerBrowserError):
    pass


class KaspiSellerUnexpectedSchema(KaspiSellerBrowserError):
    pass


@dataclass(frozen=True, slots=True)
class KaspiSellerOrderRequest:
    merchant_id: str
    order_code: str

    def __post_init__(self) -> None:
        if not self.merchant_id.strip():
            raise ValueError("merchant_id is required")
        if not self.order_code.strip():
            raise ValueError("order_code is required")


class KaspiSellerBrowserAdapter:
    """Read verified order facts through an authenticated Kaspi Seller session."""

    seller_app_url = "https://kaspi.kz/mc/#/orders"
    seller_origin = "https://mc.shop.kaspi.kz"
    graphql_url = f"{seller_origin}/mc/facade/graphql"

    def __init__(self, pool: PlaywrightBrowserPool, *, timeout_seconds: float = 30.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._pool = pool
        self._timeout_ms = int(timeout_seconds * 1000)

    @staticmethod
    def _order_detail(payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data")
        merchant = data.get("merchant") if isinstance(data, dict) else None
        detail = merchant.get("orderDetail") if isinstance(merchant, dict) else None
        if detail is None:
            raise KaspiSellerOrderNotFound("Kaspi Seller order was not found")
        if not isinstance(detail, dict):
            raise KaspiSellerUnexpectedSchema("Kaspi Seller orderDetail has unexpected shape")
        return detail

    @staticmethod
    def _variables(
        operation_name: str,
        request: KaspiSellerOrderRequest,
    ) -> dict[str, Any]:
        variables: dict[str, Any] = {
            "merchantUid": request.merchant_id,
            "orderCode": request.order_code,
        }
        if operation_name == GET_ORDER_DETAILS_OPERATION:
            variables["skipCustomerPhone"] = True
        return variables

    async def _execute_graphql(
        self,
        page: Any,
        *,
        operation_name: str,
        query: str,
        request: KaspiSellerOrderRequest,
    ) -> dict[str, Any]:
        result = await page.evaluate(
            """
            async ({ url, operationName, query, variables, timeoutMs }) => {
              const controller = new AbortController();
              const timeout = setTimeout(() => controller.abort(), timeoutMs);
              try {
                const response = await fetch(`${url}?opName=${encodeURIComponent(operationName)}`, {
                  method: "POST",
                  credentials: "include",
                  headers: {
                    "Accept": "application/json",
                    "Content-Type": "application/json"
                  },
                  body: JSON.stringify({ operationName, query, variables }),
                  signal: controller.signal
                });
                const text = await response.text();
                let body = null;
                try { body = text ? JSON.parse(text) : null; } catch (_) {}
                return {
                  ok: response.ok,
                  status: response.status,
                  finalUrl: response.url,
                  contentType: response.headers.get("content-type") || "",
                  body,
                  text: text.slice(0, 2000)
                };
              } finally {
                clearTimeout(timeout);
              }
            }
            """,
            {
                "url": self.graphql_url,
                "operationName": operation_name,
                "query": query,
                "variables": self._variables(operation_name, request),
                "timeoutMs": self._timeout_ms,
            },
        )

        if not isinstance(result, dict):
            raise KaspiSellerUnexpectedSchema("Kaspi Seller GraphQL returned no response envelope")
        status = int(result.get("status") or 0)
        final_url = str(result.get("finalUrl") or "")
        body = result.get("body")

        if status in {401, 403} or "errorpage" in final_url.casefold():
            raise KaspiSellerNotAuthorized(
                "Kaspi Seller session is not authorized; open Seller Cabinet in the agent Chrome"
            )
        if not result.get("ok"):
            raise KaspiSellerGraphQLError(
                f"Kaspi Seller GraphQL HTTP {status}: {result.get('text') or '-'}"
            )
        if not isinstance(body, dict):
            raise KaspiSellerUnexpectedSchema("Kaspi Seller GraphQL response is not JSON")
        errors = body.get("errors")
        if errors:
            raise KaspiSellerGraphQLError(f"Kaspi Seller GraphQL errors: {errors}")
        self._order_detail(body)
        return body

    async def fetch_order(self, request: KaspiSellerOrderRequest) -> dict[str, Any]:
        async with self._pool.isolated_page() as page:
            await page.goto(
                self.seller_app_url,
                wait_until="domcontentloaded",
                timeout=self._timeout_ms,
            )
            current_url = str(page.url)
            if "errorpage" in current_url.casefold() or "/mc/" not in current_url.casefold():
                raise KaspiSellerNotAuthorized(
                    "Kaspi Seller session is not authorized; open Seller Cabinet in the agent Chrome"
                )

            state_payload = await self._execute_graphql(
                page,
                operation_name=GET_ORDER_STATE_OPERATION,
                query=GET_ORDER_STATE_QUERY,
                request=request,
            )
            details_payload = await self._execute_graphql(
                page,
                operation_name=GET_ORDER_DETAILS_OPERATION,
                query=GET_ORDER_DETAILS_QUERY,
                request=request,
            )

        detail = self._order_detail(details_payload)
        result: dict[str, Any] = {
            "schema_version": "kaspi-seller-graphql-v1",
            "merchant_id": request.merchant_id,
            "order_code": request.order_code,
            "state": detail.get("state"),
            "status": detail.get("status"),
            "state_response": state_payload,
            "details_response": details_payload,
        }
        result["snapshot"] = asdict(map_seller_order_snapshot(result))
        return result
