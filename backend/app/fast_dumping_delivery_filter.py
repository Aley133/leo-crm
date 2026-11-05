"""Server-side delivery selection for ordinary physical-stock Fast pricing."""
from decimal import Decimal

from .fast_dumping_pricing import _optional_money


def _days(value, *, signed=False):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
        if Decimal(str(value)) != number:
            return None
    except (ValueError, TypeError, ArithmeticError):
        return None
    return number if (-60 if signed else 0) <= number <= 60 else None


def select_delivery_market(offers, *, own_price, page_price=None, complete=False,
                           selected_price=None):
    """Keep equal/faster/unknown deliveries and exclude confirmed slower ones.

    Older agents may select by a price premium and a multi-day threshold. A
    complete snapshot lets the server choose again without that dependency.
    Partial snapshots cannot justify an upward reprice to a different seller.
    """
    own_rows = [r for r in offers if r.get("is_own")]
    if len(own_rows) != 1 or _optional_money(own_rows[0].get("price_kzt")) != own_price:
        return None
    own_days = _days(own_rows[0].get("delivery_days"))
    # A legacy gap is usable only when no absolute delivery values contradict
    # it. New agents send both absolute dates and this derived diagnostic.
    has_context = own_days is not None or any(
        _days(r.get("delivery_gap_days"), signed=True) is not None for r in offers
    )
    if not has_context:
        return None
    headline = _optional_money(page_price)
    diagnostics, eligible, ignored = [], [], []
    for raw in offers:
        row = dict(raw)
        diagnostics.append(row)
        if row.get("is_own") or row.get("is_owned_group") or row.get("is_owned_peer"):
            continue
        price = _optional_money(row.get("price_kzt"))
        row["used_for_dumping"] = False
        if price is None or (headline is not None and price < headline):
            row["decision_reason"] = "Не выбран: цена не подтверждена для текущего buyer-context."
            continue
        days = _days(row.get("delivery_days"))
        gap = (days - own_days if own_days is not None and days is not None
               else _days(row.get("delivery_gap_days"), signed=True)
               if own_days is None and days is None else None)
        if gap is not None and gap >= 1:
            row["decision_reason"] = (
                f"Исключён: доставка позже нашей на {gap} дн. "
                "В обычном демпинге не задаёт цену независимо от разницы цен."
            )
            row["ignored_reason"] = row["decision_reason"]
            ignored.append(row)
            continue
        row["ignored_reason"] = None
        row["decision_reason"] = ("Допущен: доставка не позже нашей."
            if gap is not None else "Допущен: срок доставки не подтверждён, исключать нельзя.")
        eligible.append(row)
    eligible.sort(key=lambda r: _optional_money(r["price_kzt"]))
    selected = (eligible[0] if eligible else None) if complete else next(
        (r for r in eligible if _optional_money(r["price_kzt"]) == selected_price), None
    )
    if selected is not None:
        selected["used_for_dumping"] = True
        selected["decision_reason"] = "Выбран ценовым ориентиром. " + selected["decision_reason"]
    reason = (
        f"Фильтр доставки: исключено продавцов с доставкой позже хотя бы на день: {len(ignored)}. "
        "Одинаковая, более быстрая и неизвестная доставка остаются в сравнении."
    )
    if not complete:
        reason += " Рынок неполный: новый ориентир для повышения не выбирается."
    return {
        "offers": [r for r in diagnostics if r not in ignored],
        "diagnostics": diagnostics,
        "selected": selected,
        "ignored_count": len(ignored),
        "reason": reason,
    }
