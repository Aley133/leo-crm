from __future__ import annotations

from decimal import Decimal
from typing import Any

from . import fast_dumping_offer_runtime as offer_runtime
from .dumping_service import SUPPLIER_PREORDER_STOCK_COUNT, calculate_safe_floor
from .fast_dumping_preorder_position import decide_preorder_position


_INSTALLED = False


def _supplier_decision(*, state: Any, policy: Any, source: Any) -> dict[str, Any]:
    floor = calculate_safe_floor(
        unit_cost_kzt=source.unit_cost_kzt,
        minimum_profit_kzt=Decimal(policy.minimum_profit_kzt),
    )
    own = state.own_price_kzt or floor
    position = decide_preorder_position(
        own_price_kzt=own,
        safe_floor_kzt=floor,
        target_position=int(getattr(policy, "preorder_target_position", 4) or 4),
        undercut_step_kzt=Decimal(policy.undercut_step_kzt),
        allow_price_raise=bool(policy.allow_price_raise),
        max_undercut_gap_percent=Decimal(
            getattr(policy, "max_undercut_gap_percent", Decimal("35"))
        ),
        market_offers=getattr(state, "offers_json", None) or [],
        page_visible_price_kzt=getattr(state, "page_visible_price_kzt", None),
    )
    preorder = max(1, offer_runtime._clamp_preorder(source.delivery_days))
    status = "preorder_position" if position.exact else "preorder_position_best_effort"
    return {
        "safe_floor_kzt": offer_runtime._money(floor),
        "competitor_price_kzt": offer_runtime._money(position.anchor_price_kzt),
        "own_price_kzt": offer_runtime._money(state.own_price_kzt),
        "target_price_kzt": offer_runtime._money(position.target_price_kzt),
        "status": status,
        "reason": (
            f"FIFO закончился. Realtime предзаказ поставщика {source.name}: {preorder} дн., "
            f"виртуальный остаток {SUPPLIER_PREORDER_STOCK_COUNT}. {position.reason}"
        ),
        # Fulfilment state still has to be written even when the requested rank
        # is only best-effort. Floor/anomaly/no-raise protections are already
        # applied inside decide_preorder_position.
        "write_allowed": True,
        "stock_count": int(SUPPLIER_PREORDER_STOCK_COUNT),
        "preorder_days": preorder,
        "fulfillment_mode": "preorder",
        "source_kind": "supplier",
        "source_name": source.name,
        "pricing_status": status,
        "preorder_target_position": position.desired_position,
        "preorder_estimated_position": position.estimated_position,
        "preorder_position_exact": position.exact,
        "preorder_external_sellers": position.external_sellers,
    }


def install_supplier_pricing() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    offer_runtime._supplier_decision = _supplier_decision
