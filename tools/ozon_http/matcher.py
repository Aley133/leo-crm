from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

TOKEN = re.compile(r"[a-zа-яё0-9]+", re.I)
STOP = {
    "для", "и", "в", "на", "с", "the", "a", "an", "of", "шт", "штук",
    "таблетки", "таблеток", "табл", "капсулы", "капсул", "капс", "порошок",
    "softgels", "softgel", "tablets", "tablet", "capsules", "capsule", "pcs",
    "vitamin", "витамин", "витамины", "nutrition", "formula", "комплекс",
    "pharmaceuticals", "pharmaceutical", "pharma", "laboratories", "laboratory",
    "labs", "lab", "supplement", "supplements", "бад", "бады",
}

MARKER_ALIASES = {
    "b1": {"b1", "в1"},
    "b2": {"b2", "в2"},
    "b3": {"b3", "в3", "pp", "niacin", "ниацин"},
    "b5": {"b5", "в5"},
    "b6": {"b6", "в6"},
    "b7": {"b7", "в7", "biotin", "биотин"},
    "b9": {"b9", "в9", "folic", "фолиевая"},
    "b12": {"b12", "в12"},
    "d3": {"d3", "д3"},
    "k2": {"k2", "к2"},
    "coq10": {"coq10", "coq", "коэнзимq10", "коэнзим"},
    "omega3": {"omega3", "омега3", "omega", "омега"},
    "magnesium": {"magnesium", "магний", "магния"},
    "calcium": {"calcium", "кальций", "кальция"},
    "zinc": {"zinc", "цинк", "цинка"},
    "collagen": {"collagen", "коллаген", "collagenup"},
    "citrate": {"citrate", "цитрат", "цитрата"},
    "glycinate": {"glycinate", "глицинат", "глицината"},
    "chelated": {"chelated", "хелат", "хелатный", "хелатная"},
    "melatonin": {"melatonin", "мелатонин"},
    "selenium": {"selenium", "селен"},
    "iron": {"iron", "железо", "железа"},
    "yeast": {"yeast", "дрожжи", "дрожжей", "пивные"},
    "electrolyte": {"electrolyte", "electrolytes", "электролит", "электролиты"},
    "lutein": {"lutein", "лютеин"},
    "glucosamine": {"glucosamine", "глюкозамин"},
    "chondroitin": {"chondroitin", "хондроитин"},
}

PACK_WORD = r"(?:шт(?:ук)?|таб(?:лет(?:ок|ки)?)?|капс(?:ул(?:ы|ок)?)?|softgels?|tablets?|capsules?|pcs?)"
PACK_PATTERNS = [
    re.compile(rf"(?<![A-Za-zА-Яа-яЁё0-9])(\d{{1,4}})\s*{PACK_WORD}\b", re.I),
    re.compile(rf"\b{PACK_WORD}\s*(\d{{1,4}})(?!\d)", re.I),
]
UNIT_RE = re.compile(
    r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(mcg|мкг|μg|ug|mg|мг|kg|кг|ml|мл|iu|ме|g|гр|г)\b",
    re.I,
)
FORM_WORDS = {
    "tablet": {"tablet", "tablets", "таблетка", "таблетки", "таблеток"},
    "capsule": {"capsule", "capsules", "капсула", "капсулы", "капсул"},
    "powder": {"powder", "порошок"},
    "liquid": {"liquid", "жидкий", "жидкая", "жидкость"},
    "softgel": {"softgel", "softgels"},
}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower().replace("ё", "е")
    text = text.replace("+", " ").replace("/", " / ").replace("\\", " / ")
    return re.sub(r"\s+", " ", text).strip()


def tokens(text: str) -> set[str]:
    return {
        x.lower().replace("ё", "е")
        for x in TOKEN.findall(_norm(text))
        if len(x) > 1 and x.lower() not in STOP
    }


def _brand_tokens(brand: str | None) -> set[str]:
    if not brand:
        return set()
    return tokens(brand)


def _marker_set(text: str) -> set[str]:
    raw = tokens(text)
    joined = _norm(text).replace("-", "").replace(" ", "")
    out: set[str] = set()
    for canonical, aliases in MARKER_ALIASES.items():
        for alias in aliases:
            a = _norm(alias).replace("-", "").replace(" ", "")
            if alias in raw or (len(a) >= 2 and a in joined):
                out.add(canonical)
                break
    return out


def _pack_count(text: str) -> int | None:
    text = _norm(text)
    for rx in PACK_PATTERNS:
        m = rx.search(text)
        if m:
            try:
                value = int(m.group(1))
            except Exception:
                continue
            if 1 <= value <= 5000:
                return value
    return None


def _normalized_measure(value: float, unit: str) -> tuple[str, int]:
    u = unit.lower().replace("μ", "u")
    if u in {"mcg", "мкг", "ug"}:
        return "mcg", int(round(value))
    if u in {"mg", "мг"}:
        return "mcg", int(round(value * 1000))
    if u in {"g", "гр", "г"}:
        return "mg", int(round(value * 1000))
    if u in {"kg", "кг"}:
        return "mg", int(round(value * 1_000_000))
    if u in {"ml", "мл"}:
        return "ml", int(round(value))
    if u in {"iu", "ме"}:
        return "iu", int(round(value))
    return u, int(round(value))


def _measures(text: str) -> set[tuple[str, int]]:
    out: set[tuple[str, int]] = set()
    for raw, unit in UNIT_RE.findall(_norm(text)):
        try:
            value = float(raw.replace(",", "."))
        except ValueError:
            continue
        if value <= 0:
            continue
        out.add(_normalized_measure(value, unit))
    return out


def _forms(text: str) -> set[str]:
    raw = set(TOKEN.findall(_norm(text)))
    out: set[str] = set()
    for canonical, aliases in FORM_WORDS.items():
        if raw & aliases:
            out.add(canonical)
    return out


def extract_specs(text: str) -> dict[str, Any]:
    return {
        "pack_count": _pack_count(text),
        "measures": sorted([list(x) for x in _measures(text)]),
        "markers": sorted(_marker_set(text)),
        "forms": sorted(_forms(text)),
    }


def _measure_sets(spec: dict[str, Any]) -> set[tuple[str, int]]:
    return {(str(a), int(b)) for a, b in spec.get("measures") or []}


def _strip_brand_prefix(title: str, brand: str | None) -> str:
    text = _norm(title)
    if not brand:
        return text
    b = _norm(brand)
    if text.startswith(b):
        return text[len(b):].strip(" -,:;/")
    # Also strip the meaningful brand tokens one by one. This is useful for
    # names such as "GLS Pharmaceuticals", while Ozon often writes just "GLS".
    for tok in sorted(_brand_tokens(brand), key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(tok)}\b", " ", text, count=1, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" -,:;/")


def _core_phrase(title: str, brand: str | None = None) -> str:
    """Return the product nucleus, not the marketplace marketing tail.

    Examples:
      GLS Pharmaceuticals Пивные дрожжи (с витаминами B...) ... -> пивные дрожжи
      Solgar Calcium Citrate with Vitamin D3, Цитрат ... -> calcium citrate with vitamin d3
    """
    text = _strip_brand_prefix(title, brand)
    chunks = [x.strip() for x in re.split(r"\s*/\s*|\s*\|\s*|;", text) if x.strip()]
    first = chunks[0] if chunks else text
    first = first.split("(", 1)[0].strip(" -,:;")
    if "," in first:
        first = first.split(",", 1)[0].strip()
    # Marketing descriptions after "для ..." are usually not the product name.
    m = re.search(r"\s+для\s+", first, flags=re.I)
    if m and len(tokens(first[:m.start()])) >= 2:
        first = first[:m.start()].strip()
    # Strip pack/form tail from the compact search nucleus.
    first = re.sub(rf"\b\d{{1,4}}\s*{PACK_WORD}\b.*$", "", first, flags=re.I).strip()
    return first or text


def _core_tokens(title: str, brand: str | None = None) -> set[str]:
    return tokens(_core_phrase(title, brand)) - _brand_tokens(brand)


def _spec_analysis(query: str, title: str, brand: str | None = None, candidate_brand: str | None = None) -> dict[str, Any]:
    q = extract_specs(query)
    t = extract_specs(title)
    reasons: list[str] = []
    hard: list[str] = []
    bonus = 0.0
    penalty = 0.0

    q_count, t_count = q["pack_count"], t["pack_count"]
    if q_count and t_count:
        if q_count == t_count:
            bonus += 0.12
            reasons.append(f"упаковка совпала: {q_count}")
        else:
            hard.append(f"количество не совпало: {q_count} != {t_count}")

    q_meas = _measure_sets(q)
    t_meas = _measure_sets(t)
    if q_meas and t_meas:
        common = q_meas & t_meas
        if common:
            bonus += min(0.14, 0.07 * len(common))
            reasons.append("дозировка/масса совпала")
        q_by_kind: dict[str, set[int]] = {}
        t_by_kind: dict[str, set[int]] = {}
        for kind, value in q_meas:
            q_by_kind.setdefault(kind, set()).add(value)
        for kind, value in t_meas:
            t_by_kind.setdefault(kind, set()).add(value)
        for kind in q_by_kind.keys() & t_by_kind.keys():
            if not (q_by_kind[kind] & t_by_kind[kind]):
                hard.append(f"числовая характеристика {kind} не совпала")

    q_markers = set(q["markers"])
    t_markers = set(t["markers"])
    marker_common = q_markers & t_markers
    if marker_common:
        bonus += min(0.12, 0.025 * len(marker_common))
    missing = q_markers - t_markers
    if missing:
        # Ozon titles are frequently shorter; absence is only a mild penalty.
        penalty += min(0.06, 0.01 * len(missing))
        reasons.append("в Ozon title не видны: " + ", ".join(sorted(missing)))

    q_forms = set(q["forms"])
    t_forms = set(t["forms"])
    if q_forms and t_forms:
        if q_forms & t_forms:
            bonus += 0.03
        else:
            penalty += 0.04
            reasons.append("форма выпуска отличается")

    brand_tokens = _brand_tokens(brand)
    candidate_evidence = tokens(title) | _brand_tokens(candidate_brand)
    brand_confirmed = False
    brand_conflict = False
    brand_unconfirmed = False
    if brand_tokens:
        overlap = len(brand_tokens & candidate_evidence) / max(1, len(brand_tokens))
        if overlap >= 0.75:
            bonus += 0.18
            brand_confirmed = True
            reasons.append("бренд совпал")
        elif candidate_brand and _brand_tokens(candidate_brand):
            brand_conflict = True
            hard.append(f"бренд не совпал: {brand} != {candidate_brand}")
        else:
            brand_unconfirmed = True
            penalty += 0.04
            reasons.append("бренд не подтверждён в Ozon title")

    q_core = _core_tokens(query, brand)
    t_all = tokens(title)
    core_common = q_core & t_all
    core_recall = len(core_common) / max(1, len(q_core)) if q_core else 0.0
    core_missing = bool(len(q_core) >= 2 and not core_common)
    if q_core:
        if core_recall >= 0.80:
            bonus += 0.20
            reasons.append("ядро названия совпало")
        elif core_recall >= 0.50:
            bonus += 0.11
            reasons.append("ядро названия частично совпало")
        elif core_missing:
            penalty += 0.22
            reasons.append("ядро товара не найдено в Ozon title")
        else:
            penalty += 0.08

    return {
        "query": q,
        "candidate": t,
        "bonus": bonus,
        "penalty": penalty,
        "hard_mismatch": hard,
        "brand_confirmed": brand_confirmed,
        "brand_conflict": brand_conflict,
        "brand_unconfirmed": brand_unconfirmed,
        "core_recall": round(core_recall, 3),
        "core_missing": core_missing,
        "core_tokens": sorted(q_core),
        "reasons": reasons,
    }


def match_score(query: str, title: str, brand: str | None = None, candidate_brand: str | None = None) -> float:
    q = tokens(query)
    t = tokens(title)
    if not q or not t:
        return 0.0
    common = q & t
    recall = len(common) / len(q)
    precision = len(common) / len(t)
    f1 = 2 * recall * precision / max(1e-9, recall + precision)
    numeric_q = {x for x in q if any(ch.isdigit() for ch in x)}
    numeric_t = {x for x in t if any(ch.isdigit() for ch in x)}
    numeric_bonus = 0.0
    if numeric_q:
        numeric_bonus = 0.12 * (len(numeric_q & numeric_t) / len(numeric_q))

    nq = " ".join(sorted(q))
    nt = " ".join(sorted(t))
    fuzzy = SequenceMatcher(None, nq, nt).ratio()
    spec = _spec_analysis(query, title, brand=brand, candidate_brand=candidate_brand)

    score = (
        recall * 0.40
        + f1 * 0.15
        + fuzzy * 0.08
        + numeric_bonus
        + spec["bonus"]
        - spec["penalty"]
    )
    if spec["hard_mismatch"]:
        score = min(score, 0.49)
    elif spec.get("core_missing"):
        # Same brand/pack is not enough when the actual product nucleus is absent.
        score = min(score, 0.69)
    elif spec.get("brand_conflict") and not spec.get("brand_confirmed"):
        score = min(score, 0.79)
    return round(max(0.0, min(1.0, score)), 3)


def analyze_match(query: str, title: str, brand: str | None = None, candidate_brand: str | None = None) -> dict[str, Any]:
    spec = _spec_analysis(query, title, brand=brand, candidate_brand=candidate_brand)
    score = match_score(query, title, brand=brand, candidate_brand=candidate_brand)
    hard = list(spec["hard_mismatch"])
    if hard:
        status = "REJECT"
    elif score >= 0.80 and not spec.get("brand_conflict") and not spec.get("brand_unconfirmed"):
        status = "CONFIRMED"
    elif score >= 0.66:
        status = "REVIEW"
    else:
        status = "REJECT"
    return {
        "match_score": score,
        "text_match_score": score,
        "match_status": status,
        "hard_mismatch": hard,
        "brand_confirmed": bool(spec.get("brand_confirmed")),
        "brand_conflict": bool(spec.get("brand_conflict")),
        "brand_unconfirmed": bool(spec.get("brand_unconfirmed")),
        "core_recall": spec.get("core_recall"),
        "core_missing": bool(spec.get("core_missing")),
        "match_reasons": list(spec["reasons"]),
        "query_specs": spec["query"],
        "candidate_specs": spec["candidate"],
    }


def rank_product(product: dict[str, Any], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    query = str(product.get("title") or "")
    brand = str(product.get("brand") or "").strip() or None
    ranked: list[dict[str, Any]] = []
    for row in items:
        copy = dict(row)
        copy.update(
            analyze_match(
                query,
                str(row.get("title") or ""),
                brand=brand,
                candidate_brand=str(row.get("brand") or "").strip() or None,
            )
        )
        ranked.append(copy)
    status_rank = {"CONFIRMED": 2, "REVIEW": 1, "REJECT": 0}
    ranked.sort(
        key=lambda x: (
            status_rank.get(str(x.get("match_status")), 0),
            x.get("match_score") or 0,
            x.get("core_recall") or 0,
            x.get("rating") or 0,
            x.get("reviews") or 0,
        ),
        reverse=True,
    )
    return ranked


def rank(query: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return rank_product({"title": query, "brand": None}, items)


def _clean_query_part(part: str, brand: str, count: int | None) -> str:
    part = re.sub(r"\([^)]{12,}\)", " ", part)
    part = re.sub(r"\s+", " ", part).strip(" -,:;/")
    bits: list[str] = []
    if brand and not all(tok in tokens(part) for tok in _brand_tokens(brand)):
        bits.append(brand)
    bits.append(part)
    if count and str(count) not in part:
        bits.append(str(count))
    return " ".join(x for x in bits if x).strip()


def build_search_queries(product: dict[str, Any], max_queries: int = 5) -> list[str]:
    """Generate multiple Ozon search strategies from one noisy Kaspi title.

    We deliberately keep this simple and recall-oriented. Ozon search is good when
    it receives the product nucleus, but marketplace titles often contain long
    marketing tails. The matcher/photo layer decides whether a returned row is safe.
    """
    title = str(product.get("title") or "").strip()
    brand = str(product.get("brand") or "").strip()
    if not title:
        return []
    spec = extract_specs(title)
    count = spec.get("pack_count")
    measures = [f"{b}" for _, b in spec.get("measures") or []][:2]
    core = _core_phrase(title, brand)

    candidates: list[str] = [title]
    if core and _norm(core) != _norm(title):
        candidates.append(_clean_query_part(core, brand, count))
        if brand:
            short_brand = " ".join(sorted(_brand_tokens(brand))) or brand
            candidates.append(" ".join(x for x in [short_brand, core, str(count) if count else ""] if x))

    # Bilingual/slash titles: try each side independently.
    for part in [x.strip() for x in re.split(r"\s*/\s*|\\|\|", title) if x.strip()][:2]:
        candidates.append(_clean_query_part(part, brand, count))

    markers = sorted(_marker_set(title))
    if brand and markers:
        compact = " ".join([brand, *markers, *(measures[:1]), *([str(count)] if count else [])])
        candidates.append(compact)

    # A brand + core-token query is intentionally broad and often finds the exact
    # Ozon card when the long title was interpreted too literally.
    core_tokens = [x for x in TOKEN.findall(_norm(core)) if len(x) > 1 and x not in STOP]
    if brand and core_tokens:
        candidates.append(" ".join([brand, *core_tokens[:5], *([str(count)] if count else [])]))

    out: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        item = re.sub(r"\s+", " ", item).strip()
        key = _norm(item)
        if item and key not in seen:
            seen.add(key)
            out.append(item)
        if len(out) >= max(1, int(max_queries)):
            break
    return out
