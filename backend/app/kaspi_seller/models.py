from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SellerOrderStep:
    step: str
    actual_time: str | None = None
    planned_time: str | None = None
    timeout_time: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SellerOrderStep":
        return cls(
            step=str(payload.get("step") or "").strip().upper(),
            actual_time=_optional_text(payload.get("actualTime")),
            planned_time=_optional_text(payload.get("plannedTime")),
            timeout_time=_optional_text(payload.get("timeoutTime")),
        )


@dataclass(frozen=True, slots=True)
class SellerOrderFacts:
    order_code: str | None
    state: str
    status: str
    preorder: bool
    is_order_arrived: bool
    kd_assembled: bool
    kd_transmitted_to_courier: bool
    steps: tuple[SellerOrderStep, ...]
    marker_names: tuple[str, ...]

    def step_actual_time(self, step_name: str) -> str | None:
        wanted = step_name.strip().upper()
        for step in self.steps:
            if step.step == wanted:
                return step.actual_time
        return None


def _optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)
