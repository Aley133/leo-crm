"""A shared, bounded price cycle for the owner's shops in rocket mode."""
from datetime import UTC, datetime
from decimal import Decimal
from sqlalchemy import select
from .fast_dumping_pricing import _optional_money


def owned_rows(offers):
    return [r for r in offers if (r.get('is_own') or r.get('is_owned_group') or r.get('is_owned_peer'))
            and (_optional_money(r.get('price_kzt')) or 0) > 0]


def participants(offers):
    rows = owned_rows(offers)
    # Exact identities are required to keep each shop's one-tenge offset stable.
    if len(rows) != 2 or not all(r.get('merchant_id') for r in rows):
        return []
    ids = [str(r['merchant_id']) for r in rows]
    return ids if len(set(ids)) == 2 and sum(bool(r.get('is_own')) for r in rows) == 1 else []


def read_shared_cycle(db, *, policy, state):
    from .models import Product
    from .fast_dumping_models import FastDumpingPolicy, FastDumpingState
    ids = participants(state.offers_json or [])
    if not ids:
        return {}
    local = dict((state.automation_json or {}).get('owned_cycle') or {})
    product = db.get(Product, policy.product_id)
    cycles = [local]
    if product and product.kaspi_product_id:
        # One read of the same Kaspi card, city and zone across owned workspaces.
        # No peer row locks or writes: agents keep their existing single-flight leases.
        rows = db.execute(select(FastDumpingState.automation_json).join(
            FastDumpingPolicy, FastDumpingPolicy.id == FastDumpingState.policy_id
        ).join(Product, Product.id == FastDumpingPolicy.product_id).where(
            Product.kaspi_product_id == product.kaspi_product_id,
            FastDumpingPolicy.enabled.is_(True), FastDumpingPolicy.pricing_mode == 'automation',
            FastDumpingPolicy.city_id == policy.city_id, FastDumpingPolicy.zone_id == policy.zone_id,
        ).execution_options(include_all_workspaces=True)).all()
        cycles += [dict((data or {}).get('owned_cycle') or {}) for data, in rows]
    valid = [c for c in cycles if set(c.get('participants', [])) == set(ids) and c.get('version') == 1]
    return dict(max(valid, key=lambda c: c.get('phase_changed_at', ''))) if valid else {}


def cycle_price(*, offers, own, floor, ceiling, cycle, result, profit, observed_at=None, now=None):
    ids = participants(offers)
    if not ids:
        return None
    now = now or datetime.now(UTC)
    phase_time = observed_at or now
    if phase_time.tzinfo is None:
        phase_time = phase_time.replace(tzinfo=UTC)
    rows = owned_rows(offers)
    own_row = next(r for r in rows if r.get('is_own'))
    peer = next(r for r in rows if not r.get('is_own'))
    peer_price = _optional_money(peer['price_kzt'])
    if cycle.get('version') != 1 or set(cycle.get('participants', [])) != set(ids):
        ordered = sorted(rows, key=lambda r: (_optional_money(r['price_kzt']), str(r['merchant_id'])))
        cycle.clear()
        cycle.update(version=1, participants=[str(r['merchant_id']) for r in ordered],
                     base_kzt=str(max(min(own, peer_price), max(own, peer_price) - 1)), phase='raise', phase_changed_at=phase_time.isoformat())
    elif observed_at is not None:
        changed = datetime.fromisoformat(cycle['phase_changed_at'])
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        if observed_at < changed:
            return result(own, 'owned_cycle_sync', 'Соседний магазин сменил фазу цикла; ждём свежий рынок.')
    base = Decimal(cycle['base_kzt'])
    if min(own, peer_price) > base + 101:
        # Genuine market increases become a new start; reset itself cannot drift it.
        base = max(min(own, peer_price), max(own, peer_price) - 1)
        cycle.update(base_kzt=str(base), phase='raise', phase_changed_at=phase_time.isoformat())
    slot = cycle['participants'].index(str(own_row['merchant_id']))
    upper = min(base + 100 + slot, ceiling)
    # A logistics tariff jump must not turn a 100-tenge raise into lower net profit.
    from .fast_dumping_pricing import LOGISTICS_PRICE_BREAKS
    candidates = [upper] + [b - 1 for b in LOGISTICS_PRICE_BREAKS if max(base, floor) <= b - 1 <= upper]
    upper = max(candidates, key=lambda p: (profit(p), p))
    boundary = max(base, floor)
    if upper <= boundary:
        # External pressure or floor leaves no profitable corridor. Never follow a peer below it.
        target = max(floor, min(own, ceiling))
        return result(target, 'owned_peer_guard', 'Коридор +100 ₸ ограничен внешним рынком или floor; взаимное снижение остановлено.', target != own)
    expected = {}
    for i, identity in enumerate(cycle['participants']):
        top = min(base + 100 + i, ceiling)
        choices = [top] + [b - 1 for b in LOGISTICS_PRICE_BREAKS if max(base, floor) <= b - 1 <= top]
        expected[identity] = max(choices, key=lambda p: (profit(p), p))
    if cycle['phase'] == 'raise' and all(_optional_money(r['price_kzt']) >= expected[str(r['merchant_id'])] for r in rows):
        cycle.update(phase='step', phase_changed_at=phase_time.isoformat())
    elif cycle['phase'] == 'step' and min(own, peer_price) <= boundary:
        cycle.update(phase='raise', phase_changed_at=phase_time.isoformat())
    if cycle['phase'] == 'raise':
        target = max(floor, min(ceiling, max(own, upper)))
        return result(target, 'owned_group_reset',
                      f'Общий цикл BARWORK/LeoXpress: старт {base} ₸, подъём на 100 ₸; цель {target} ₸. Ждём оба магазина наверху.', target != own)
    external_ahead = any(not (r.get('is_own') or r.get('is_owned_group') or r.get('is_owned_peer'))
                         and _optional_money(r.get('price_kzt')) is not None
                         and _optional_money(r['price_kzt']) <= own for r in offers)
    if not external_ahead:
        target = max(floor, min(own, ceiling))
        return result(target, 'owned_peer_guard', 'Чужие продавцы не требуют шага вниз; сохраняем маржу собственных магазинов.', target != own)
    target = max(boundary, min(upper, own, peer_price - 1)) if own >= peer_price else max(boundary, min(own, ceiling))
    return result(target, 'owned_group_band',
                  f'Борьба с внешними продавцами: шаг 1 ₸ между своими магазинами внутри {base}–{base + 100} ₸. На нижней границе общий возврат вверх.', target != own)
