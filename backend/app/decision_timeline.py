from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Iterable, Literal

from .supplier_intelligence import BestOfferEngine, SupplierCandidate


DecisionEventType = Literal[
    "initial_leader",
    "leader_changed",
    "leader_reaffirmed",
    "no_decision",
]


@dataclass(frozen=True, slots=True)
class TimelineBinding:
    binding_id: int
    supplier_product_id: int
    supplier_code: str
    supplier_name: str
    is_primary: bool
    priority: int


@dataclass(frozen=True, slots=True)
class TimelineObservation:
    observation_id: int
    supplier_product_id: int
    price: Decimal | None
    currency: str | None
    available: bool | None
    delivery_days: int | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class DecisionTimelineEntry:
    observation_id: int
    occurred_at: datetime
    event_type: DecisionEventType
    leader_binding_id: int | None
    leader_supplier_code: str | None
    leader_supplier_name: str | None
    previous_binding_id: int | None
    previous_supplier_code: str | None
    previous_supplier_name: str | None
    leader_score: Decimal | None
    score_gap: Decimal | None
    confidence: str
    price_delta: Decimal | None
    delivery_delta: int | None
    reason: str


@dataclass(frozen=True, slots=True)
class _OfferSnapshot:
    price: Decimal | None
    currency: str | None
    available: bool | None
    delivery_days: int | None
    observed_at: datetime


class DecisionTimelineProjector:
    """Rebuild historical supplier decisions from immutable observations.

    This is a read model. It does not write monitoring state and therefore keeps
    Browser Runtime, Observation Engine and Supplier Intelligence independent.
    """

    @classmethod
    def project(
        cls,
        bindings: Iterable[TimelineBinding],
        observations: Iterable[TimelineObservation],
        *,
        include_reaffirmed: bool = True,
    ) -> tuple[DecisionTimelineEntry, ...]:
        binding_rows = tuple(bindings)
        binding_by_product = {row.supplier_product_id: row for row in binding_rows}
        ordered = sorted(
            (row for row in observations if row.supplier_product_id in binding_by_product),
            key=lambda row: (cls._aware(row.observed_at), row.observation_id),
        )
        states: dict[int, _OfferSnapshot] = {}
        entries: list[DecisionTimelineEntry] = []
        previous_binding_id: int | None = None

        for observation in ordered:
            occurred_at = cls._aware(observation.observed_at)
            states[observation.supplier_product_id] = _OfferSnapshot(
                price=observation.price,
                currency=observation.currency,
                available=observation.available,
                delivery_days=observation.delivery_days,
                observed_at=occurred_at,
            )
            candidates = cls._candidates(binding_rows, states)
            decision = BestOfferEngine.decide(candidates, now=occurred_at)
            best = decision.best

            if best is None:
                if previous_binding_id is not None or not entries:
                    previous = cls._binding_by_id(binding_rows, previous_binding_id)
                    entries.append(
                        DecisionTimelineEntry(
                            observation_id=observation.observation_id,
                            occurred_at=occurred_at,
                            event_type="no_decision",
                            leader_binding_id=None,
                            leader_supplier_code=None,
                            leader_supplier_name=None,
                            previous_binding_id=previous_binding_id,
                            previous_supplier_code=None if previous is None else previous.supplier_code,
                            previous_supplier_name=None if previous is None else previous.supplier_name,
                            leader_score=None,
                            score_gap=None,
                            confidence=decision.confidence,
                            price_delta=None,
                            delivery_delta=None,
                            reason="Нет доступных предложений с подтверждённой ценой",
                        )
                    )
                previous_binding_id = None
                continue

            leader = cls._binding_by_id(binding_rows, best.binding_id)
            if leader is None:
                continue

            if previous_binding_id is None:
                event_type: DecisionEventType = "initial_leader"
                reason = "Первый подтверждённый лидер"
            elif previous_binding_id != best.binding_id:
                event_type = "leader_changed"
                reason = cls._change_reason(
                    new_binding_id=best.binding_id,
                    previous_binding_id=previous_binding_id,
                    bindings=binding_rows,
                    states=states,
                )
            else:
                if not include_reaffirmed:
                    continue
                event_type = "leader_reaffirmed"
                changed_binding = binding_by_product[observation.supplier_product_id]
                reason = f"Лидер сохранился после обновления {changed_binding.supplier_name}"

            previous = cls._binding_by_id(binding_rows, previous_binding_id)
            price_delta, delivery_delta = cls._deltas(
                new_binding_id=best.binding_id,
                previous_binding_id=previous_binding_id,
                bindings=binding_rows,
                states=states,
            )
            entries.append(
                DecisionTimelineEntry(
                    observation_id=observation.observation_id,
                    occurred_at=occurred_at,
                    event_type=event_type,
                    leader_binding_id=best.binding_id,
                    leader_supplier_code=leader.supplier_code,
                    leader_supplier_name=leader.supplier_name,
                    previous_binding_id=previous_binding_id,
                    previous_supplier_code=None if previous is None else previous.supplier_code,
                    previous_supplier_name=None if previous is None else previous.supplier_name,
                    leader_score=best.total_score,
                    score_gap=decision.score_gap,
                    confidence=decision.confidence,
                    price_delta=price_delta,
                    delivery_delta=delivery_delta,
                    reason=reason,
                )
            )
            previous_binding_id = best.binding_id

        return tuple(reversed(entries))

    @classmethod
    def _candidates(
        cls,
        bindings: tuple[TimelineBinding, ...],
        states: dict[int, _OfferSnapshot],
    ) -> tuple[SupplierCandidate, ...]:
        rows: list[SupplierCandidate] = []
        for binding in bindings:
            state = states.get(binding.supplier_product_id)
            rows.append(
                SupplierCandidate(
                    binding_id=binding.binding_id,
                    supplier_product_id=binding.supplier_product_id,
                    supplier_code=binding.supplier_code,
                    supplier_name=binding.supplier_name,
                    price=None if state is None else state.price,
                    currency=None if state is None else state.currency,
                    available=None if state is None else state.available,
                    delivery_days=None if state is None else state.delivery_days,
                    is_primary=binding.is_primary,
                    priority=binding.priority,
                    last_checked_at=None if state is None else state.observed_at,
                )
            )
        return tuple(rows)

    @classmethod
    def _change_reason(
        cls,
        *,
        new_binding_id: int,
        previous_binding_id: int,
        bindings: tuple[TimelineBinding, ...],
        states: dict[int, _OfferSnapshot],
    ) -> str:
        new_binding = cls._binding_by_id(bindings, new_binding_id)
        previous = cls._binding_by_id(bindings, previous_binding_id)
        if new_binding is None or previous is None:
            return "Лучший поставщик изменился"
        new_state = states.get(new_binding.supplier_product_id)
        previous_state = states.get(previous.supplier_product_id)
        if previous_state is None or previous_state.available is not True:
            return f"{previous.supplier_name} больше не имеет доступного предложения"
        if new_state is not None and new_state.price is not None and previous_state.price is not None:
            saving = previous_state.price - new_state.price
            if saving > 0:
                return f"{new_binding.supplier_name} дешевле на {cls._money(saving)}"
        if (
            new_state is not None
            and new_state.delivery_days is not None
            and previous_state.delivery_days is not None
            and new_state.delivery_days < previous_state.delivery_days
        ):
            delta = previous_state.delivery_days - new_state.delivery_days
            return f"{new_binding.supplier_name} доставляет быстрее на {delta} дн."
        return f"{new_binding.supplier_name} получил более высокий рейтинг предложения"

    @classmethod
    def _deltas(
        cls,
        *,
        new_binding_id: int,
        previous_binding_id: int | None,
        bindings: tuple[TimelineBinding, ...],
        states: dict[int, _OfferSnapshot],
    ) -> tuple[Decimal | None, int | None]:
        if previous_binding_id is None or previous_binding_id == new_binding_id:
            return None, None
        new_binding = cls._binding_by_id(bindings, new_binding_id)
        previous = cls._binding_by_id(bindings, previous_binding_id)
        if new_binding is None or previous is None:
            return None, None
        new_state = states.get(new_binding.supplier_product_id)
        previous_state = states.get(previous.supplier_product_id)
        if new_state is None or previous_state is None:
            return None, None
        price_delta = (
            None
            if new_state.price is None or previous_state.price is None
            else new_state.price - previous_state.price
        )
        delivery_delta = (
            None
            if new_state.delivery_days is None or previous_state.delivery_days is None
            else new_state.delivery_days - previous_state.delivery_days
        )
        return price_delta, delivery_delta

    @staticmethod
    def _binding_by_id(
        bindings: tuple[TimelineBinding, ...], binding_id: int | None
    ) -> TimelineBinding | None:
        if binding_id is None:
            return None
        return next((row for row in bindings if row.binding_id == binding_id), None)

    @staticmethod
    def _money(value: Decimal) -> str:
        return f"{value.quantize(Decimal('0.01')):,.2f} ₸".replace(",", " ")

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
