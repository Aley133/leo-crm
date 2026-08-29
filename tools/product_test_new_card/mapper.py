from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

STOP_WORDS = {
    "товар", "характеристики", "общие", "основные", "other", "general", "product",
    "для", "the", "and", "или", "of", "тип", "type",
    # Too generic for enum selection and was making values such as
    # "для очищения организма" look deceptively close to
    # "для тонуса и укрепления организма".
    "организм", "организма", "организму",
}
SYNONYMS = {
    "страна изготовитель": "страна производства",
    "страна производитель": "страна производства",
    "производитель страна": "страна производства",
    "количество в упаковке": "количество штук в упаковке",
    "количество в упаковке шт": "количество штук в упаковке",
    "количество штук": "количество штук в упаковке",
    "штук в упаковке": "количество штук в упаковке",
    "количество капсул": "количество штук в упаковке",
    "количество капсул в упаковке": "количество штук в упаковке",
    "количество таблеток": "количество штук в упаковке",
    "количество таблеток в упаковке": "количество штук в упаковке",
    "форма продукта": "форма выпуска",
    "форма выпуска продукта": "форма выпуска",
    "лекарственная форма": "форма выпуска",
    "возраст": "рекомендуемый возраст",
    "минимальный возраст": "рекомендуемый возраст",
    "минимальный возраст от": "рекомендуемый возраст",
    "возраст применения": "рекомендуемый возраст",
    "пол": "для кого",
    "целевой пол": "для кого",
    "кому подходит": "для кого",
    "основной ингредиент": "основной компонент",
    "активный ингредиент": "основной компонент",
    "действующее вещество": "основной компонент",
    "активный компонент": "основной компонент",
    "свидетельство о государственной регистрации": "номер сгр",
    "номер свидетельства о государственной регистрации": "номер сгр",
    "регистрационный номер": "номер сгр",
    "сгр": "номер сгр",
    "наименование препарата": "название препарата",
    "наименование": "название препарата",
    "направление витаминов": "назначение",
    "направление бад": "назначение",
    "назначение бад": "назначение",
    "форма выпуска бад": "форма выпуска",
    "количество в упаковке шт": "количество штук в упаковке",
    "вес товара г": "вес",
    "масса нетто г": "вес",
    "объем мл": "объем жидкости",
    "объём мл": "объем жидкости",
    "объем": "объем жидкости",
    "объём": "объем жидкости",
    "цвет товара": "цвет",
    "вес товара": "вес",
    "масса": "вес",
    "объем товара": "объем жидкости",
    "объём товара": "объем жидкости",
    "модель товара": "модель",
}

# Exact source preference for Kaspi supplement attributes.  This is deliberately
# based on field meaning, not fuzzy similarity, so a random Ozon string can no
# longer land in e.g. SGR, volume or number-of-packages.
TARGET_SOURCE_ALIASES: dict[str, tuple[str, ...]] = {
    "vitamins*drug name": ("Название препарата", "Основной компонент"),
    "vitamins*purpose": ("Назначение", "Направление БАД", "Направление витаминов", "Область применения"),
    "dietary supplements*range of applications": ("Область применения", "Назначение", "Направление БАД", "Направление витаминов"),
    "dietary supplements*main component": ("Основной компонент",),
    "vitamins*registration status": ("Регистрационный статус", "Противопоказания БАД"),
    "dietary supplements*sgr number": ("Номер СГР", "СГР"),
    "dietary supplements*recommended age": ("Рекомендуемый возраст", "Минимальный возраст от"),
    "vitamins*gender": ("Для кого", "Целевая аудитория"),
    "vitamins*taste": ("Вкус", "Вкусовой акцент (вкус)"),
    "vitamins*liquid volume": ("Объем жидкости", "Объем, мл", "Объём, мл"),
    "vitamins*dosage form": ("Форма выпуска продукта", "Форма выпуска"),
    "vitamins*number of packages": ("Количество упаковок", "Количество штук в упаковке"),
    "dietary supplements*amount": ("Количество", "Количество штук в упаковке", "Количество в упаковке, шт", "Количество капсул", "Количество таблеток"),
    "dietary supplements*release form": ("Форма выпуска", "Форма выпуска продукта", "Форма выпуска БАД"),
    "dietary supplements*number of packages": ("Количество упаковок",),
    "pharmacy*country": ("Страна производитель", "Страна-изготовитель", "Страна производства"),
    "vitamins*indications for use": ("Показания к применению", "Область применения", "Назначение", "Направление БАД"),
    "dietary supplements*indications for use": ("Показания к применению", "Область применения", "Назначение", "Направление БАД"),
    "vitamins*recommendations": ("Рекомендации по применению",),
    "dietary supplements*recommendations": ("Рекомендации по применению",),
    # Ozon's "Противопоказания БАД" is usually the legal disclaimer
    # "БАД. НЕ ЯВЛЯЕТСЯ ЛЕКАРСТВЕННЫМ СРЕДСТВОМ", not a medical
    # contraindication.  It is useful for registration status, but must not be
    # copied into Kaspi contraindications.
    "vitamins*contraindications": ("Противопоказания",),
    "dietary supplements*contraindications": ("Противопоказания",),
    "dietary supplements*special instructions": ("Особые указания", "Дополнительная информация"),
    "dietary supplements*additional information": ("Дополнительная информация",),
    "vitamins*composition": ("Состав",),
    "dietary supplements*composition": ("Состав",),
}

# Concept words used only when Kaspi returns an enum dictionary.  They improve
# matching between different wording on Ozon and Kaspi without manufacturing a
# value that is not in Kaspi's own allowed list.
CONCEPT_ALIASES: dict[str, tuple[str, ...]] = {
    "микрофлор": ("кишечник", "пищеварение", "пищеварительная система", "жкт"),
    "кишечник": ("микрофлора", "пищеварение", "пищеварительная система", "жкт"),
    "пищевар": ("кишечник", "жкт", "микрофлора"),
    "тонус": ("общеукрепляющее", "укрепление организма", "энергия", "бодрость"),
    "укреплен": ("общеукрепляющее", "тонус", "иммунитет"),
    "иммун": ("укрепление организма", "общеукрепляющее"),
    "взросл": ("универсальные", "взрослые"),
    "жидк": ("жидкость", "раствор"),
}

# Field-specific semantic bridges for Kaspi enum dictionaries.  The generic
# fuzzy matcher is intentionally conservative; these rules only activate for a
# known Kaspi field and only choose a value that Kaspi itself returned in the
# allowed-values list.  This is what lets Ozon's
# "Для восстановления микрофлоры кишечника" map to Kaspi's compact
# "пищеварение" without inventing a new enum value.
FIELD_ENUM_CONCEPTS: dict[str, tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]] = {
    "dietary supplements*range of applications": (
        (("микрофлор", "кишеч", "пищевар", "жкт", "желуд"), ("пищевар", "кишеч", "жкт", "желуд")),
        (("сердц", "кровообращ", "сосуд"), ("сердц", "кровообращ", "сосуд")),
        (("иммун", "простуд"), ("иммун", "простуд")),
        (("нерв", "стресс", "сон"), ("нерв", "стресс", "сон")),
        (("печен", "детокс", "очищ"), ("печен", "детокс", "очищ")),
        (("сустав", "кост", "опорно"), ("сустав", "кост", "опорно")),
        (("кожа", "волос", "ногт"), ("кожа", "волос", "ногт")),
    ),
    "vitamins*purpose": (
        (("микрофлор", "кишеч", "пищевар", "жкт"), ("микрофлор", "кишеч", "пищевар", "жкт")),
        (("тонус", "укреплен", "общеукреп"), ("тонус", "укреплен", "общеукреп")),
        (("очищ", "детокс"), ("очищ", "детокс")),
        (("иммун" ,), ("иммун",)),
        (("сердц", "сосуд", "кровообращ", "кардио"), ("сердц", "сосуд", "кровообращ", "кардио")),
    ),
    "dietary supplements*release form": (
        (("капсул",), ("капсул",)),
        (("таблет",), ("таблет",)),
        (("порош",), ("порош",)),
        (("жидк", "раствор"), ("жидк", "раствор")),
    ),
}


def _contains_stem(text: Any, stems: tuple[str, ...]) -> bool:
    value = normalize(text)
    return any(stem in value for stem in stems)


def _field_enum_boost(raw_value: str, field_code: str, enum_code: str, enum_name: str) -> float:
    rules = FIELD_ENUM_CONCEPTS.get(str(field_code or "").casefold(), ())
    if not rules:
        return 0.0
    target = f"{enum_name} {enum_code}"
    for source_stems, target_stems in rules:
        if _contains_stem(raw_value, source_stems) and _contains_stem(target, target_stems):
            return 0.96
    return 0.0


def normalize(text: Any) -> str:
    value = str(text or "").casefold().replace("ё", "е")
    value = value.replace("*", " ").replace("_", " ").replace(".", " ")
    value = re.sub(r"[^a-zа-я0-9%+]+", " ", value)
    words = [word for word in value.split() if word not in STOP_WORDS]
    value = " ".join(words).strip()
    return SYNONYMS.get(value, value)


def tokens(text: Any) -> set[str]:
    return {part for part in normalize(text).split() if len(part) > 1}


def _semantic_tokens(text: Any) -> set[str]:
    raw = normalize(text)
    out = tokens(raw)
    for token in list(out):
        for stem, aliases in CONCEPT_ALIASES.items():
            if stem in token:
                for alias in aliases:
                    out.update(tokens(alias))
    return out


def similarity(left: Any, right: Any) -> float:
    a = normalize(left)
    b = normalize(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = _semantic_tokens(a), _semantic_tokens(b)
    overlap = len(ta & tb) / max(1, len(ta | tb))
    seq = SequenceMatcher(None, a, b).ratio()
    contains = 0.94 if a in b or b in a else 0.0
    return max(seq * 0.68 + overlap * 0.32, contains)


def attribute_label(attribute: dict[str, Any]) -> str:
    title = str(attribute.get("title") or "").strip()
    code = str(attribute.get("code") or "").strip()
    tail = code.rsplit("*", 1)[-1].replace(".", " ").replace("_", " ").strip()
    # Kaspi's classification endpoint often has no separate Russian title and the
    # UI therefore sees the code as title.  In that case the tail is more useful.
    if title and title != code and "*" not in title:
        return title
    return tail or title or code


def _age_number(text: Any) -> int | None:
    m = re.search(r"(?<!\d)(\d{1,2})(?!\d)", str(text or ""))
    if not m:
        return None
    value = int(m.group(1))
    return value if 0 <= value <= 99 else None


def _enum_score(raw_value: str, code: str, name: str, *, field_code: str = "") -> float:
    score = max(similarity(raw_value, name), similarity(raw_value, code))
    score = max(score, _field_enum_boost(raw_value, field_code, code, name))
    raw_age = _age_number(raw_value)
    enum_age = _age_number(name or code)
    if raw_age is not None and enum_age is not None:
        if raw_age == enum_age:
            score = max(score, 0.99)
        elif abs(raw_age - enum_age) == 1:
            score = max(score, 0.50)
    raw_norm = normalize(raw_value)
    name_norm = normalize(name)
    code_norm = normalize(code)
    if raw_norm and (raw_norm == name_norm or raw_norm == code_norm):
        score = 1.0
    return score


def best_value(
    raw_value: str,
    allowed: list[dict[str, str]],
    *,
    field_code: str = "",
) -> tuple[str, float]:
    if not allowed:
        return raw_value, 0.0
    scored: list[tuple[float, str]] = []
    for row in allowed:
        code = str(row.get("code") or "").strip()
        name = str(row.get("name") or code).strip()
        score = _enum_score(raw_value, code, name, field_code=field_code)
        scored.append((score, code or name))
    scored.sort(reverse=True)
    score, value = scored[0]
    # Never invent an enum.  It must be an actual value from Kaspi and have a
    # meaningful match to the Ozon fact.
    if score < 0.48:
        return "", score
    return value, score


def _split_source_values(raw: str) -> list[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    # Ozon multi-values are normally separated by commas/semicolons/newlines.
    parts = [x.strip(" .") for x in re.split(r"[;\n]+|,\s+(?=[А-ЯA-Z])", text) if x.strip(" .")]
    return parts or [text]


def best_values(
    raw_value: str,
    allowed: list[dict[str, str]],
    *,
    multi: bool,
    field_code: str = "",
) -> tuple[str, float]:
    if not multi:
        return best_value(raw_value, allowed, field_code=field_code)
    if not allowed:
        return "; ".join(_split_source_values(raw_value)), 0.0
    chosen: list[str] = []
    scores: list[float] = []
    for part in _split_source_values(raw_value):
        value, score = best_value(part, allowed, field_code=field_code)
        if value and value not in chosen:
            chosen.append(value)
            scores.append(score)
    # If splitting did not help, compare the whole phrase once.
    if not chosen:
        value, score = best_value(raw_value, allowed, field_code=field_code)
        if value:
            chosen.append(value)
            scores.append(score)
    return "; ".join(chosen), (max(scores) if scores else 0.0)


def _source_index(characteristics: list[dict[str, str]]) -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    for index, row in enumerate(characteristics):
        key = normalize(row.get("name"))
        if key:
            result.setdefault(key, []).append(index)
    return result


def _preferred_source_candidates(
    code: str,
    label: str,
    source: list[dict[str, str]],
    source_by_name: dict[str, list[int]],
    used_source: set[int],
) -> list[tuple[int, float, bool]]:
    candidates: list[tuple[int, float, bool]] = []
    seen: set[int] = set()
    aliases = TARGET_SOURCE_ALIASES.get(code.casefold(), ())
    # Exact semantic aliases are allowed to be reused by more than one Kaspi
    # field.  One Ozon fact can legitimately feed both purpose/application.
    for rank, alias in enumerate(aliases):
        for index in source_by_name.get(normalize(alias), []):
            if index in seen:
                continue
            seen.add(index)
            candidates.append((index, max(0.90, 1.0 - rank * 0.02), True))

    fuzzy: list[tuple[float, int]] = []
    for index, row in enumerate(source):
        if index in used_source or index in seen:
            continue
        score = similarity(row.get("name"), label)
        if score >= 0.42:
            fuzzy.append((score, index))
    fuzzy.sort(reverse=True)
    candidates.extend((index, score, False) for score, index in fuzzy[:8])
    return candidates

def map_characteristics(
    characteristics: list[dict[str, str]],
    attributes: list[dict[str, Any]],
    values_by_code: dict[str, list[dict[str, str]]] | None = None,
) -> list[dict[str, Any]]:
    values_by_code = values_by_code or {}
    source = [row for row in characteristics if row.get("name") and row.get("value")]
    source_by_name = _source_index(source)
    output: list[dict[str, Any]] = []
    used_source: set[int] = set()

    for attribute in attributes:
        code = str(attribute.get("code") or "").strip()
        label = attribute_label(attribute)
        allowed = values_by_code.get(code, [])
        mapped_value = ""
        value_score = 0.0
        source_name = None
        source_value = None
        chosen_score = 0.0
        chosen_index: int | None = None
        chosen_explicit = False

        for index, field_score, explicit in _preferred_source_candidates(
            code, label, source, source_by_name, used_source
        ):
            row = source[index]
            candidate_value, candidate_value_score = best_values(
                str(row.get("value") or ""),
                allowed,
                multi=bool(attribute.get("multi_valued")),
                field_code=code,
            )
            # Keep diagnostics for the strongest attempted source even if an enum
            # dictionary rejects its value.  The UI can then show what Ozon supplied.
            if source_name is None:
                source_name = row.get("name")
                source_value = row.get("value")
                chosen_score = field_score
                value_score = candidate_value_score
            # If Kaspi gives an enum dictionary, try the next semantic source when
            # this one does not map to an allowed value.  This is important for
            # fields like application area where Ozon exposes several useful texts.
            if allowed and not candidate_value:
                continue
            mapped_value = candidate_value
            value_score = candidate_value_score
            source_name = row.get("name")
            source_value = row.get("value")
            chosen_score = field_score
            chosen_index = index
            chosen_explicit = explicit
            break

        if chosen_index is not None and not chosen_explicit:
            used_source.add(chosen_index)

        output.append(
            {
                "code": code,
                "title": attribute.get("title") or code,
                "required": bool(attribute.get("required")),
                "type": attribute.get("type"),
                "multi_valued": bool(attribute.get("multi_valued")),
                "value": mapped_value,
                "source_name": source_name,
                "source_value": source_value,
                "match_score": round(chosen_score, 3),
                "value_score": round(value_score, 3),
                "allowed_values": allowed[:100],
            }
        )
    return output


DESCRIPTION_LABELS: dict[str, str] = {
    "vitamins*drug name": "Название препарата",
    "vitamins*purpose": "Назначение",
    "dietary supplements*range of applications": "Область применения",
    "dietary supplements*main component": "Основной компонент",
    "vitamins*registration status": "Регистрационный статус",
    "dietary supplements*sgr number": "Номер СГР",
    "dietary supplements*recommended age": "Рекомендуемый возраст",
    "vitamins*gender": "Для кого",
    "vitamins*taste": "Вкус",
    "vitamins*liquid volume": "Объем",
    "vitamins*dosage form": "Форма выпуска",
    "dietary supplements*amount": "Количество",
    "dietary supplements*release form": "Форма выпуска",
    "vitamins*number of packages": "Количество упаковок",
    "dietary supplements*number of packages": "Количество упаковок",
    "pharmacy*country": "Страна-производитель",
}


def ensure_kaspi_description(
    description: str,
    *,
    title: str,
    brand: str,
    attributes: list[dict[str, Any]],
    min_len: int = 100,
    max_len: int = 1024,
) -> str:
    """Guarantee a factual Kaspi description of at least 100 characters.

    Kaspi's detailed import result is the source of truth: it currently rejects
    descriptions shorter than 100 characters.  The lab keeps the conservative
    1024-character ceiling used by its official-schema integration, while using
    only the user-visible Ozon/Kaspi facts already present in the form.
    """
    raw = str(description or "").strip()
    if len(raw) >= min_len:
        return raw[:max_len]

    chunks: list[str] = []
    seen: set[str] = set()

    def add(text: Any) -> None:
        value = str(text or "").strip()
        if not value:
            return
        key = value.casefold().replace("ё", "е")
        if key in seen:
            return
        seen.add(key)
        current = "\n".join(chunks)
        remaining = max_len - len(current) - (1 if current else 0)
        if remaining <= 0:
            return
        if len(value) > remaining:
            if remaining < 24:
                return
            value = value[:remaining].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
        chunks.append(value)

    if raw:
        add(raw)
    add(f"Товар: {str(title or '').strip()}." if str(title or '').strip() else "")
    add(f"Бренд: {str(brand or '').strip()}." if str(brand or '').strip() else "")

    for row in attributes:
        code = str(row.get("code") or "").strip()
        label = DESCRIPTION_LABELS.get(code.casefold())
        if not label:
            continue
        # Prefer the original Ozon fact for human-readable prose; enum `value`
        # may be an internal Kaspi code on some categories.
        value = str(row.get("source_value") or row.get("value") or "").strip()
        if not value:
            continue
        value = re.sub(r"[;\n]+", ", ", value)
        add(f"{label}: {value}.")

    # Generic category fallback: use the visible attribute title/source value
    # rather than an internal enum code. This keeps the description factual for
    # non-BAD categories too.
    if len("\n".join(chunks)) < min_len:
        for row in attributes:
            title_text = str(row.get("title") or row.get("source_name") or "").strip()
            value = str(row.get("source_value") or row.get("value") or "").strip()
            if not title_text or not value:
                continue
            if "*" in title_text and not row.get("source_name"):
                continue
            value = re.sub(r"[;\n]+", ", ", value)
            add(f"{title_text}: {value}.")
            if len("\n".join(chunks)) >= min_len:
                break

    # Very small synthetic/test products may still have fewer than 100 chars of
    # factual data. Add a neutral non-medical sentence instead of letting LIVE
    # fail remotely. Production Ozon cards normally reach the limit before this.
    if len("\n".join(chunks)) < min_len:
        add("Основные сведения приведены по характеристикам товара и данным производителя.")

    return "\n".join(chunks).strip()[:max_len]


def build_payload(
    *,
    sku: str,
    title: str,
    brand: str,
    category: str,
    description: str,
    attributes: list[dict[str, Any]],
    images: list[str],
    weight: str | None = None,
) -> dict[str, Any]:
    def attribute_value(row: dict[str, Any]) -> Any:
        raw = row.get("value")
        text = str(raw or "").strip()
        if row.get("multi_valued"):
            parts = [part.strip() for part in re.split(r"[;\n]+", text) if part.strip()]
            return parts
        kind = str(row.get("type") or "").casefold()
        if kind == "boolean":
            low = text.casefold().replace("ё", "е")
            if low in {"true", "1", "да", "yes"}:
                return True
            if low in {"false", "0", "нет", "no"}:
                return False
        if kind == "number":
            candidate = text.replace(" ", "").replace(",", ".")
            # Ozon may provide a unit suffix ("500 мл", "18 лет").  Kaspi number
            # attributes need just the numeric portion.
            match = re.search(r"[-+]?\d+(?:\.\d+)?", candidate)
            if match:
                try:
                    number = float(match.group(0))
                    return int(number) if number.is_integer() else number
                except ValueError:
                    pass
        return text

    final_description = ensure_kaspi_description(
        str(description or ""),
        title=str(title or ""),
        brand=str(brand or ""),
        attributes=attributes,
    )

    product: dict[str, Any] = {
        "sku": str(sku).strip(),
        "title": str(title).strip(),
        "brand": str(brand).strip(),
        "category": str(category).strip(),
        "description": final_description,
        "attributes": [
            {"code": str(row.get("code") or "").strip(), "value": attribute_value(row)}
            for row in attributes
            if str(row.get("code") or "").strip() and str(row.get("value") or "").strip()
        ],
        "images": [
            {"url": str(url).strip()}
            for url in images
            if str(url).strip().startswith("https://")
        ][:20],
    }
    if weight is not None and str(weight).strip():
        product["weight"] = str(weight).strip()
    return product


def validate_payload(product: dict[str, Any], mapped_attributes: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for key in ("sku", "title", "brand", "category", "description"):
        if not str(product.get(key) or "").strip():
            errors.append(f"Missing required field: {key}")
    if len(str(product.get("sku") or "")) > 64:
        errors.append("SKU is longer than 64 characters")
    description_len = len(str(product.get("description") or ""))
    if 0 < description_len < 100:
        errors.append("Description must be at least 100 characters for Kaspi import")
    if description_len > 1024:
        errors.append("Description is longer than 1024 characters")
    if not product.get("images"):
        errors.append("At least one image URL is required for this lab workflow")
    filled_codes = {str(row.get("code") or "") for row in product.get("attributes") or []}
    for row in mapped_attributes:
        if row.get("required") and str(row.get("code") or "") not in filled_codes:
            errors.append(f"Required Kaspi attribute is empty: {row.get('title') or row.get('code')}")
    return errors
