from __future__ import annotations

from decimal import Decimal
from typing import Any

from . import fast_dumping_offer_runtime as offer_runtime
from .dumping_service import SUPPLIER_PREORDER_STOCK_COUNT, calculate_safe_floor
from .fast_dumping_pricing import decide_fast_price


_INSTALLED = False


def _supplier_decision(*, state: Any, policy: Any, source: Any) -> dict[str, Any]:
    floor = calculate_safe_floor(
        unit_cost_kzt=source.unit_cost_kzt,
        minimum_profit_kzt=Decimal(policy.minimum_profit_kzt),
    )
    own = state.own_price_kzt or floor
    decision = decide_fast_price(
        own_price_kzt=own,
        competitor_price_kzt=state.competitor_price_kzt,
        safe_floor_kzt=floor,
        undercut_step_kzt=Decimal(policy.undercut_step_kzt),
        allow_price_raise=bool(policy.allow_price_raise),
        max_undercut_gap_percent=Decimal(
            getattr(policy, "max_undercut_gap_percent", Decimal("35"))
        ),
        market_offers=getattr(state, "offers_json", None) or [],
        delivery_price_premium_kzt=getattr(
            policy, "delivery_price_premium_kzt", 500
        ),
        delivery_advantage_days=getattr(policy, "delivery_advantage_days", 3),
        page_visible_price_kzt=getattr(state, "page_visible_price_kzt", None),
    )
    target = decision.target_price_kzt or own
    preorder = max(1, offer_runtime._clamp_preorder(source.delivery_days))
    return {
        "safe_floor_kzt": offer_runtime._money(floor),
        "competitor_price_kzt": offer_runtime._money(state.competitor_price_kzt),
        "own_price_kzt": offer_runtime._money(state.own_price_kzt),
        "target_price_kzt": offer_runtime._money(target),
        "status": decision.status if decision.write_allowed else "preorder_ready",
        "reason": (
            f"FIFO закончился. Realtime предзаказ поставщика {source.name}: {preorder} дн., "
            f"виртуальный остаток {SUPPLIER_PREORDER_STOCK_COUNT}. {decision.reason}"
        ),
        "write_allowed": True,
        "stock_count": int(SUPPLIER_PREORDER_STOCK_COUNT),
        "preorder_days": preorder,
        "fulfillment_mode": "preorder",
        "source_kind": "supplier",
        "source_name": source.name,
        "pricing_status": decision.status,
    }


def install_supplier_pricing() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    offer_runtime._supplier_decision = _supplier_decision
