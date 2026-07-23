from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class CommerceOrderLineRead(BaseModel):
    line_id: int
    product_id: int | None
    external_product_id: str | None
    merchant_sku: str | None
    title: str
    quantity: int
    unit_price: Decimal
    line_total: Decimal
    is_resolved: bool
    purchase_request_id: str | None
    purchase_status: str | None
    purchase_version: int | None
    procurement_state: str
    procurement_unit_cost: Decimal | None
    procurement_total_cost: Decimal | None
    procurement_source_name: str | None
    gross_margin: Decimal | None
    gross_margin_pct: Decimal | None
    kaspi_commission: Decimal | None
    tax: Decimal | None
    logistics: Decimal
    net_profit: Decimal | None
    net_margin_pct: Decimal | None


class CommerceOrderRead(BaseModel):
    order_id: int
    external_code: str | None
    marketplace: str
    marketplace_account_id: int | None
    marketplace_external_account_id: str | None
    status: str
    original_status: str
    operational_stage: str
    operational_stage_source: str
    currency: str
    total_amount: Decimal
    ordered_at: datetime | None
    delivered_at: datetime | None
    units: int
    unresolved_lines: int
    procurement_required_lines: int
    lines: list[CommerceOrderLineRead]


class CommerceSummaryRead(BaseModel):
    orders_count: int
    units_count: int
    revenue: Decimal
    confirmed_net_profit: Decimal
    confirmed_profit_units: int
    active_orders: int
    delivered_orders: int
    cancelled_orders: int
    unresolved_lines: int
    procurement_required_lines: int


class CommerceOrdersResponse(BaseModel):
    total: int
    limit: int
    offset: int
    summary: CommerceSummaryRead
    items: list[CommerceOrderRead]
