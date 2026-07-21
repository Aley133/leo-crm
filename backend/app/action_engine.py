from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from .supplier_intelligence import BestOfferDecision, SupplierCandidate


ActionKind = Literal[
    "no_action",
    "switch_supplier",
    "manual_review",
    "collect_more_data",
    "no_available_offer",
]
ActionSeverity = Literal["success", "info", "warning", "critical"]


@dataclass(frozen=True, slots=True)
class ActionRecommendation:
    kind: ActionKind
    severity: ActionSeverity
    title: str
    summary: str
    reasons: tuple[str, ...]
    target_binding_id: int | None = None
    target_supplier_code: str | None = None
    target_supplier_name: str | None = None
    score_gap: Decimal | None = None
    auto_apply_allowed: bool = False


class ActionEngine:
    """Translate a Best Offer decision into a safe, explainable recommendation.

    The engine does not mutate bindings, XML or marketplace state. Automatic
    execution remains disabled until Pricing Policy and risk controls exist.
    """

    @classmethod
    def recommend(
        cls,
        candidates: tuple[SupplierCandidate, ...],
        decision: BestOfferDecision,
    ) -> ActionRecommendation:
        by_binding = {candidate.binding_id: candidate for candidate in candidates}
        primary = next((candidate for candidate in candidates if candidate.is_primary), None)
        best = decision.best

        if best is None:
            return ActionRecommendation(
                kind="no_available_offer",
                severity="critical",
                title="Нет доступного поставщика",
                summary="CRM не нашла предложение с подтверждёнными ценой и наличием.",
                reasons=decision.warnings or ("Нужно проверить привязки и повторить мониторинг",),
            )

        winner = by_binding.get(best.binding_id)
        if winner is None:
            return ActionRecommendation(
                kind="manual_review",
                severity="critical",
                title="Требуется ручная проверка",
                summary="Решение ссылается на неизвестную привязку поставщика.",
                reasons=("Нарушена целостность текущего состояния карточки",),
            )

        if decision.confidence == "low":
            reasons = list(decision.warnings)
            if not reasons:
                reasons.append("Недостаточный разрыв между предложениями")
            return ActionRecommendation(
                kind="collect_more_data" if decision.runner_up is None else "manual_review",
                severity="warning",
                title="Не применять решение автоматически",
                summary=(
                    "Нужно дождаться дополнительных наблюдений."
                    if decision.runner_up is None
                    else "Лучшие предложения слишком близки или данные неполны."
                ),
                reasons=tuple(reasons),
                target_binding_id=winner.binding_id,
                target_supplier_code=winner.supplier_code,
                target_supplier_name=winner.supplier_name,
                score_gap=decision.score_gap,
            )

        if primary is None:
            return ActionRecommendation(
                kind="switch_supplier",
                severity="info",
                title="Назначить основного поставщика",
                summary=f"Рекомендуемый поставщик: {winner.supplier_name}.",
                reasons=(
                    "У товара не назначена основная привязка",
                    *best.reasons,
                ),
                target_binding_id=winner.binding_id,
                target_supplier_code=winner.supplier_code,
                target_supplier_name=winner.supplier_name,
                score_gap=decision.score_gap,
            )

        if primary.binding_id != winner.binding_id:
            return ActionRecommendation(
                kind="switch_supplier",
                severity="warning",
                title="Рекомендуется сменить основного поставщика",
                summary=f"{winner.supplier_name} сейчас лучше текущего основного поставщика {primary.supplier_name}.",
                reasons=(
                    f"Рейтинг победителя: {best.total_score}",
                    f"Уверенность решения: {decision.confidence}",
                    *best.reasons,
                ),
                target_binding_id=winner.binding_id,
                target_supplier_code=winner.supplier_code,
                target_supplier_name=winner.supplier_name,
                score_gap=decision.score_gap,
            )

        return ActionRecommendation(
            kind="no_action",
            severity="success",
            title="Действий не требуется",
            summary=f"Текущий основной поставщик {winner.supplier_name} остаётся лучшим.",
            reasons=best.reasons,
            target_binding_id=winner.binding_id,
            target_supplier_code=winner.supplier_code,
            target_supplier_name=winner.supplier_name,
            score_gap=decision.score_gap,
        )
