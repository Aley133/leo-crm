from __future__ import annotations

import html
import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

OZON_HOSTS = {"ozon.ru", "www.ozon.ru", "ozon.kz", "www.ozon.kz"}
IMAGE_HOST_MARKERS = ("ozone.ru", "ozon.ru", "ozon.kz", "cdn")
BLOCK_MARKERS = (
    "captcha",
    "incidentid",
    "access denied",
    "доступ ограничен",
    "подтвердите, что вы не робот",
    "проверка безопасности",
)
JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)
META_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']([^"\']+)["\'][^>]+content=["\']([^"\']*)["\'][^>]*>',
    re.I,
)
URL_RE = re.compile(r"https://[^\s\"'<>\\]+", re.I)


def _ozon_host(host: str | None) -> bool:
    h = (host or "").lower().strip().rstrip(".")
    return h in OZON_HOSTS or h.endswith(".ozon.ru") or h.endswith(".ozon.kz")


def _jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except Exception:
        return value


def _walk(value: Any, path: str = "") -> Iterable[tuple[str, Any]]:
    value = _jsonish(value)
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from _walk(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]"
            yield from _walk(child, child_path)


def _clean_text(value: Any, max_len: int = 4000) -> str | None:
    if not isinstance(value, str):
        return None
    text = html.unescape(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    return text[:max_len]


def _first_by_keys(root: Any, keys: set[str], *, min_len: int = 1, max_len: int = 1024) -> str | None:
    wanted = {k.casefold() for k in keys}
    candidates: list[tuple[int, str]] = []
    for path, node in _walk(root):
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if str(key).casefold() not in wanted:
                continue
            text = _clean_text(value, max_len=max_len)
            if not text or len(text) < min_len:
                continue
            depth = path.count(".") + path.count("[")
            candidates.append((depth, text))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], -len(x[1])))
    return candidates[0][1]


def _rich_text(value: Any, max_len: int = 1200) -> str | None:
    """Flatten Ozon rich-text nodes used by current composer widgets.

    Modern ``webShortCharacteristics`` / ``webCharacteristics`` rows do not
    necessarily expose ``{name, value}``.  A very common shape is instead
    ``title.textRs[]`` + ``values[]`` (or ``contentRS`` / ``valueRs``), where
    each item is a tiny rich-text node containing ``text`` or ``content``.
    Treat those wrappers as presentation only and recover the visible text.
    """
    value = _jsonish(value)
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, str):
        return _clean_text(value, max_len)
    if isinstance(value, list):
        parts: list[str] = []
        for item in value[:40]:
            txt = _rich_text(item, max_len=max_len)
            if txt and txt not in parts:
                parts.append(txt)
        return ", ".join(parts)[:max_len] or None
    if isinstance(value, dict):
        # Ozon's rich-string containers. Prefer these before generic keys so a
        # title object does not collapse into an unrelated tracking property.
        for key in ("textRs", "textRS", "contentRS", "contentRs", "valueRs", "valueRS", "values"):
            if key in value:
                txt = _rich_text(value[key], max_len=max_len)
                if txt:
                    return txt
        for key in ("text", "content", "value", "name", "label", "title"):
            if key in value:
                txt = _rich_text(value[key], max_len=max_len)
                if txt:
                    return txt
    return None


def _value_text(value: Any) -> str | None:
    # Keep the old helper name because the generic parser calls it in many
    # places, but let it understand the current Ozon rich-text representation.
    return _rich_text(value, 500)


_SPEC_BLOCKLIST = {
    "цена", "price", "скидка", "discount", "рейтинг", "rating", "отзывы", "reviews",
    "доставка", "delivery", "продавец", "seller", "артикул продавца", "sku",
    # Ozon widget/UI service fields that are not product characteristics.
    "actiontype", "behavior", "color", "font", "goto", "iconcolor", "iconname",
    "key", "limit", "link", "linktocharacteristics", "linktodescription",
    "linktocharacteristicsbuttontext", "linktodescriptionbuttontext",
    "linktoexternaltext", "offsetscrol", "offsetscroll", "id", "type",
    "isfreshcolorsenabled", "iswidgetcharacteristicsonpage",
    "iswidgetdescriptiononpage", "disablepaidbrand",
}

_TECHNICAL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(?:[A-Z][a-zA-Z0-9_]*)+$")
_RESIZE_SEGMENT_RE = re.compile(r"^(?:wc|c|w|h)\d+(?:x\d+)?$", re.I)


def _meaningful_spec_name(name: str) -> bool:
    value = str(name or "").strip(" :")
    if len(value) < 2 or len(value) > 100:
        return False
    low = value.casefold().replace("ё", "е")
    compact = re.sub(r"[^a-zа-я0-9]+", "", low)
    if low in _SPEC_BLOCKLIST or compact in _SPEC_BLOCKLIST:
        return False
    # camelCase widget properties are overwhelmingly internal metadata.
    if _TECHNICAL_NAME_RE.match(value):
        return False
    return True


def clean_product_title(value: str | None) -> str | None:
    text = _clean_text(value, 1024)
    if not text:
        return None
    # Ozon appends an SEO tail which is unsuitable for a Kaspi product name.
    text = re.sub(
        r"\s+(?:купить|заказать)\s+на\s+OZON\b.*$",
        "",
        text,
        flags=re.I,
    ).strip(" ,.-")
    text = re.sub(r"\s*\(\d{6,}\)\s*$", "", text).strip(" ,.-")
    return text[:1024] or None


# Labels that are valuable for Kaspi catalog creation.  Ozon can expose them in
# several shapes: structured characteristic rows, HTML-ish description widgets,
# or long text blocks where a label and value are on adjacent lines.
_FACT_LABELS = (
    "Артикул",
    "Тип",
    "Название препарата",
    "Основной компонент",
    "Страна-изготовитель",
    "Страна изготовитель",
    "Страна производства",
    "Номер СГР",
    "СГР",
    "Объем, мл",
    "Объём, мл",
    "Целевая аудитория",
    "Вкусовой акцент (вкус)",
    "Вкус",
    "Форма выпуска продукта",
    "Форма выпуска",
    "Количество в упаковке, шт",
    "Количество штук в упаковке",
    "Количество упаковок",
    "Противопоказания БАД",
    "Срок годности в днях",
    "Для детей",
    "Минимальный возраст от",
    "Рекомендуемый возраст",
    "Направление витаминов",
    "Направление БАД",
    "Назначение",
    "Вес товара, г",
    "Вес, г",
    "Масса нетто, г",
    "Количество капсул",
    "Количество таблеток",
    "Количество пастилок",
    "Форма выпуска БАД",
    "Дозировка",
    "Состав",
    "Рекомендации по применению",
    "Продолжительность приема",
    "Противопоказания",
    "Условия хранения",
    "Срок годности",
    "Область применения",
)
def _fact_label_key(value: Any) -> str:
    text = html.unescape(str(value or "")).casefold().replace("ё", "е")
    text = text.replace("\xa0", " ")
    text = re.sub(r"[\[\](){}]", " ", text)
    text = re.sub(r"\bшт\.?(?=\s|$)", "шт", text)
    text = re.sub(r"\s*[,;:]\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip(" .:-–—")
    return text


_FACT_LABEL_LOOKUP: dict[str, str] = {}
for _fact_label in _FACT_LABELS:
    _FACT_LABEL_LOOKUP.setdefault(_fact_label_key(_fact_label), _fact_label)

# Real Ozon cards vary wording between categories and even between two widgets
# of the same product.  These aliases normalize wording only; values themselves
# are still taken from Ozon and are never fabricated here.
_FACT_LABEL_ALIASES = {
    "количество в упаковке": "Количество в упаковке, шт",
    "количество в упаковке шт": "Количество в упаковке, шт",
    "количество единиц в упаковке": "Количество в упаковке, шт",
    "количество капсул в упаковке": "Количество капсул",
    "количество таблеток в упаковке": "Количество таблеток",
    "количество капсул/таблеток": "Количество штук в упаковке",
    "количество капсул таблеток": "Количество штук в упаковке",
    "форма выпуска товара": "Форма выпуска продукта",
    "форма продукта": "Форма выпуска продукта",
    "форма выпуска препарата": "Форма выпуска продукта",
    "возрастная аудитория": "Целевая аудитория",
    "аудитория": "Целевая аудитория",
    "минимальный возраст": "Минимальный возраст от",
    "возраст от": "Минимальный возраст от",
    "страна происхождения": "Страна-изготовитель",
    "страна производства": "Страна производства",
    "направление применения": "Направление БАД",
    "назначение бад": "Направление БАД",
    "свидетельство о государственной регистрации": "Номер СГР",
    "номер свидетельства о государственной регистрации": "Номер СГР",
}
for _alias, _canonical in _FACT_LABEL_ALIASES.items():
    _FACT_LABEL_LOOKUP.setdefault(_fact_label_key(_alias), _canonical)

_FACT_LABELS_BY_LEN = sorted(tuple(_FACT_LABELS) + tuple(_FACT_LABEL_ALIASES), key=len, reverse=True)


def _clean_multiline_text(value: Any, max_len: int = 16000) -> str | None:
    if not isinstance(value, str):
        return None
    text = html.unescape(value)
    text = text.replace("\\n", "\n").replace("\\r", "\n")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(?:p|div|li|h[1-6])>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = text.replace("&#x20;", " ")
    lines: list[str] = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip(" -•*#\t")
        if not line or line.casefold() == "svg":
            continue
        lines.append(line)
    text = "\n".join(lines).strip()
    return text[:max_len] or None


def _canonical_fact_label(label: str) -> str | None:
    return _FACT_LABEL_LOOKUP.get(_fact_label_key(label))


def _add_fact(out: list[dict[str, str]], seen_names: set[str], name: str, value: Any) -> None:
    canonical = _canonical_fact_label(name) or str(name or "").strip(" :")
    text = _clean_text(value, 1200) if isinstance(value, str) else _value_text(value)
    if not canonical or not text:
        return
    text = text.strip(" :;,-")
    if not text or text.casefold() in {"svg", canonical.casefold()}:
        return
    norm = canonical.casefold().replace("ё", "е")
    if norm in seen_names:
        return
    seen_names.add(norm)
    out.append({"name": canonical, "value": text[:1200]})


def extract_labeled_facts(root: Any) -> list[dict[str, str]]:
    """Extract exact product facts even when Ozon wraps them in text widgets."""
    out: list[dict[str, str]] = []
    seen_names: set[str] = set()

    # Explicit dict keys are the safest source and may appear outside a path named
    # 'characteristics', so scan the whole payload only for our exact trusted labels.
    for _, node in _walk(root):
        if not isinstance(node, dict):
            continue
        for key, raw in node.items():
            canonical = _canonical_fact_label(str(key))
            if canonical:
                _add_fact(out, seen_names, canonical, raw)

    # Ozon description/features widgets can contain a long pre-rendered text blob.
    # Parse both "Label: value" and "Label\nvalue" forms.
    for _, node in _walk(root):
        if not isinstance(node, str):
            continue
        blob = _clean_multiline_text(node)
        if not blob or len(blob) < 4:
            continue
        lines = [x.strip() for x in blob.splitlines() if x.strip()]
        if not lines:
            continue
        for idx, line in enumerate(lines):
            # Exact label on one line, value on the next line.
            canonical = _canonical_fact_label(line.rstrip(":"))
            if canonical:
                value = None
                for nxt in lines[idx + 1: idx + 4]:
                    if _canonical_fact_label(nxt.rstrip(":")):
                        break
                    if any(re.match(rf"^{re.escape(label)}\s*[:–—-]", nxt, flags=re.I) for label in _FACT_LABELS_BY_LEN):
                        break
                    if nxt.casefold() == "svg":
                        continue
                    value = nxt
                    break
                if value:
                    _add_fact(out, seen_names, canonical, value)
                continue

            # "Label: value" within one line.  Try the longest labels first so
            # "Противопоказания БАД" wins over "Противопоказания".
            for label in _FACT_LABELS_BY_LEN:
                m = re.match(rf"^{re.escape(label)}\s*[:–—-]\s*(.+)$", line, flags=re.I)
                if m:
                    _add_fact(out, seen_names, label, m.group(1))
                    break

    return out


def _fact_index(characteristics: list[dict[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in characteristics:
        name = str(row.get("name") or "").strip()
        value = str(row.get("value") or "").strip()
        if not name or not value:
            continue
        out.setdefault(name.casefold().replace("ё", "е"), value)
    return out


def _pick_fact(index: dict[str, str], *names: str) -> str | None:
    for name in names:
        value = index.get(name.casefold().replace("ё", "е"))
        if value:
            return value
    return None


def _title_pack_count(title: str | None) -> str | None:
    text = str(title or "")
    # Explicit quantity markers are much safer than a bare number in a supplement
    # title (which can be dosage, concentration or volume).
    patterns = (
        r"\b(\d{1,4})\s*шт\.?\b",
        r"\b(\d{1,4})\s*(?:капс(?:ул\w*)?\.?|табл(?:ет\w*)?\.?|драже|пастил\w*|леден\w*)",
        r"\b(\d{1,3})\s*(?:флакон(?:а|ов)?|бутыл(?:ка|ки|ок)|упаков(?:ка|ки|ок)|бан(?:ка|ки|ок))\b",
    )
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.I)
        if m:
            return m.group(1)
    return None


def enrich_supplement_characteristics(
    title: str | None,
    characteristics: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Build a stable semantic supplement layer on top of heterogeneous Ozon labels.

    Ozon uses different names for the same fact across liquid/solid BAD cards
    (for example ``Направление витаминов`` vs ``Направление БАД``).  Kaspi,
    meanwhile, uses another set of category-specific attribute codes.  This
    function preserves every raw Ozon row for diagnostics, but adds canonical
    helper facts that the mapper can rely on consistently.
    """
    output = list(characteristics)
    index = _fact_index(output)

    def add(name: str, value: str | None) -> None:
        if not value:
            return
        key = name.casefold().replace("ё", "е")
        if key in index:
            return
        value = str(value).strip()
        if not value:
            return
        output.append({"name": name, "value": value})
        index[key] = value

    # ---- canonical aliases shared by all supplement cards ----
    main_component = _pick_fact(
        index,
        "Основной компонент",
        "Основной ингредиент",
        "Активный компонент",
        "Действующее вещество",
    )
    add("Название препарата", _pick_fact(index, "Название препарата") or main_component)

    purpose = _pick_fact(
        index,
        "Назначение",
        "Направление БАД",
        "Направление витаминов",
        "Показания к применению",
    )
    add("Назначение", purpose)
    # Range/application is frequently represented by the same Ozon direction
    # text. Kaspi enum dictionaries will reduce it to a legal allowed value.
    add("Область применения", _pick_fact(index, "Область применения") or purpose)

    country = _pick_fact(index, "Страна производитель", "Страна-изготовитель", "Страна изготовления", "Страна производства")
    add("Страна производитель", country)

    form = _pick_fact(
        index,
        "Форма выпуска",
        "Форма выпуска продукта",
        "Форма выпуска БАД",
        "Лекарственная форма",
    )
    # Backstop for cards where Ozon keeps the detailed characteristic only in a
    # widget shape that has drifted again.  The product title often still has a
    # precise physical-form marker ("120 капс.", "60 таблеток", etc.).
    if not form:
        title_low = str(title or "").casefold().replace("ё", "е")
        if re.search(r"\bкапс(?:ул\w*)?(?:\.|\b)", title_low):
            form = "Капсулы"
        elif re.search(r"\bтабл(?:ет\w*)?(?:\.|\b)", title_low):
            form = "Таблетки"
        elif "драже" in title_low:
            form = "Драже"
        elif "пастил" in title_low or "леден" in title_low:
            form = "Пастилки"
        elif any(token in title_low for token in ("порошок", "порошк", "гранул")):
            form = "Порошок"
        elif any(token in title_low for token in ("жидк", "сироп", "раствор", "капли", "флакон", "бутыл")):
            form = "Жидкость"
    add("Форма выпуска", form)

    volume = _pick_fact(index, "Объем жидкости", "Объем, мл", "Объём, мл")
    add("Объем жидкости", volume)
    add("Вкус", _pick_fact(index, "Вкус", "Вкусовой акцент (вкус)"))

    # SGR is a literal identifier: normalize only the field name, never the value.
    add("Номер СГР", _pick_fact(index, "Номер СГР", "СГР"))

    bad_status = _pick_fact(index, "Противопоказания БАД") or ""
    if "не является лекарственным" in bad_status.casefold().replace("ё", "е"):
        add("Регистрационный статус", "не является лекарственным средством")

    # ---- audience / age ----
    explicit_age = _pick_fact(index, "Рекомендуемый возраст", "Минимальный возраст от")
    audience_raw = _pick_fact(index, "Целевая аудитория", "Возрастная категория") or ""
    audience = audience_raw.casefold().replace("ё", "е")
    for_children_raw = _pick_fact(index, "Для детей") or ""
    for_children = for_children_raw.casefold().replace("ё", "е")
    if explicit_age:
        add("Рекомендуемый возраст", explicit_age)
    elif any(token in audience for token in ("взросл", "adult")) and for_children in {"", "нет", "false", "0"}:
        # Ozon often omits a numeric minimum age while explicitly classifying
        # the product as adult. Kaspi's adult enum is 18+, so expose that
        # semantic fact and let the enum mapper choose Kaspi's exact code.
        add("Рекомендуемый возраст", "18 лет")

    if any(token in audience for token in ("взросл", "adult")) and for_children in {"", "нет", "false", "0"}:
        add("Для кого", "универсальные")

    # ---- physical form / quantity ----
    form_norm = (form or "").casefold().replace("ё", "е")
    title_norm = str(title or "").casefold().replace("ё", "е")
    solid = any(token in f"{form_norm} {title_norm}" for token in (
        "капсул", "таблет", "драже", "пастил", "леденц", "жеватель",
    ))
    liquid = any(token in f"{form_norm} {title_norm}" for token in (
        "жидк", "сироп", "раствор", "капл", "флакон", "бутыл",
    ))

    explicit_piece_qty = _pick_fact(
        index,
        "Количество штук в упаковке",
        "Количество капсул",
        "Количество таблеток",
        "Количество пастилок",
    )
    ozon_pack_qty = _pick_fact(index, "Количество в упаковке, шт")
    pack_count = _title_pack_count(title)

    if solid:
        # For capsules/tablets Ozon's package quantity is normally the real
        # number of physical units and is exactly what Kaspi Amount needs.
        piece_qty = explicit_piece_qty or ozon_pack_qty or pack_count
        add("Количество штук в упаковке", piece_qty)
        add("Количество", piece_qty)
        add("Количество упаковок", _pick_fact(index, "Количество упаковок") or "1")
    elif liquid:
        # Liquid BAD cards occasionally publish servings as "Количество в
        # упаковке, шт" (e.g. 300 for one 500 ml bottle). Prefer explicit title
        # package count; otherwise accept only a plausible physical count.
        existing_qty = explicit_piece_qty or ozon_pack_qty
        if not pack_count:
            match = re.search(r"\d+", str(existing_qty or ""))
            qty_number = int(match.group(0)) if match else None
            pack_count = str(qty_number) if qty_number is not None and 1 <= qty_number <= 20 else "1"
        if not explicit_piece_qty:
            add("Количество штук в упаковке", pack_count)
        add("Количество упаковок", _pick_fact(index, "Количество упаковок") or pack_count)
    else:
        piece_qty = explicit_piece_qty or ozon_pack_qty or pack_count
        add("Количество штук в упаковке", piece_qty)
        add("Количество", piece_qty)
        add("Количество упаковок", _pick_fact(index, "Количество упаковок") or "1")

    # ---- logistics weight ----
    weight_g = _pick_fact(index, "Вес товара, г", "Вес, г", "Масса нетто, г")
    if weight_g:
        match = re.search(r"\d+(?:[.,]\d+)?", weight_g.replace(" ", ""))
        if match:
            grams = float(match.group(0).replace(",", "."))
            if grams > 0:
                kg = grams / 1000.0
                add("Вес для расчета логистики, кг", (f"{kg:.3f}").rstrip("0").rstrip("."))

    return output

def build_semantic_description(
    characteristics: list[dict[str, str]],
    fallback: str | None,
    *,
    title: str | None = None,
    brand: str | None = None,
    min_len: int = 100,
    max_len: int = 1024,
) -> str:
    """Build a factual Kaspi-safe description from Ozon product data.

    Kaspi's real import validator currently requires 100-7000 characters, while
    the public import schema used by this lab has historically advertised a
    smaller ceiling.  We therefore keep a conservative 1024-character cap but
    *always* try to reach 100 characters using only product facts already
    extracted from Ozon.  No marketplace metadata or invented medical claims are
    added.
    """
    index = _fact_index(characteristics)

    def clean_fallback(value: str | None) -> str:
        cleaned = _clean_text(value, max_len) or ""
        low = cleaned.casefold()
        if any(marker in low for marker in (
            "ozon по выгодным ценам",
            "msapplication-",
            "интернет-магазин ozon",
            "application-name",
            "browserconfig.xml",
        )):
            return ""
        return cleaned

    chunks: list[str] = []
    seen: set[str] = set()

    def add(label: str, value: str | None, *, bullet: bool = False) -> None:
        value = str(value or "").strip()
        if not value:
            return
        key = f"{label.casefold()}::{value.casefold()}"
        if key in seen:
            return
        seen.add(key)
        line = f"{label}: {value}" if label else value
        if bullet:
            line = f"- {line}"
        current = "\n".join(chunks)
        sep = 1 if current else 0
        remaining = max_len - len(current) - sep
        if remaining <= 0:
            return
        if len(line) > remaining:
            if remaining >= 24:
                clipped = line[:remaining].rsplit(" ", 1)[0].rstrip(" ,;:")
                if clipped:
                    line = clipped + "…"
            else:
                return
        chunks.append(line)

    # Prefer real narrative sections when Ozon exposes them.
    for label in (
        "Дозировка",
        "Состав",
        "Рекомендации по применению",
        "Продолжительность приема",
        "Противопоказания",
        "Область применения",
        "Условия хранения",
        "Срок годности",
    ):
        add(label, _pick_fact(index, label))

    # Then add structured facts.  This is also the normal path for terse Ozon
    # cards such as Omega-3 where the /features page has enough data for Kaspi
    # but little or no long-form description.
    for label in (
        "Основной компонент",
        "Назначение",
        "Рекомендуемый возраст",
        "Для кого",
        "Объем жидкости",
        "Количество штук в упаковке",
        "Количество",
        "Форма выпуска",
        "Вкус",
        "Страна производитель",
        "Номер СГР",
    ):
        add(label, _pick_fact(index, label), bullet=True)

    # A clean Ozon narrative can help if the fact list is still sparse.
    cleaned = clean_fallback(fallback)
    if cleaned and len("\n".join(chunks)) < min_len:
        add("Описание", cleaned)

    # Title/brand are factual and ensure even very sparse products satisfy the
    # Kaspi 100-character lower bound without fabricating properties.
    if len("\n".join(chunks)) < min_len:
        add("Товар", clean_product_title(title or ""))
    if len("\n".join(chunks)) < min_len:
        add("Бренд", _clean_text(brand, 256))

    # Last-resort factual rows from the normalized characteristic list.
    if len("\n".join(chunks)) < min_len:
        for row in characteristics:
            name = str(row.get("name") or "").strip()
            value = str(row.get("value") or "").strip()
            if not name or not value or not _meaningful_spec_name(name):
                continue
            low_name = name.casefold()
            if any(bad in low_name for bad in ("application-name", "msapplication", "browserconfig")):
                continue
            add(name, value, bullet=True)
            if len("\n".join(chunks)) >= min_len:
                break

    return "\n".join(chunks).strip()[:max_len]


# Backwards-compatible private alias used by older tests/imports.
def _semantic_description(characteristics: list[dict[str, str]], fallback: str | None) -> str:
    return build_semantic_description(characteristics, fallback)



def extract_logistics_weight_kg(characteristics: list[dict[str, str]]) -> str | None:
    index = _fact_index(characteristics)
    value = _pick_fact(index, "Вес для расчета логистики, кг")
    if value:
        return value
    raw = _pick_fact(index, "Вес товара, г", "Вес, г", "Масса нетто, г")
    if not raw:
        return None
    match = re.search(r"\d+(?:[.,]\d+)?", raw.replace(" ", ""))
    if not match:
        return None
    grams = float(match.group(0).replace(",", "."))
    if grams <= 0:
        return None
    return (f"{grams / 1000.0:.3f}").rstrip("0").rstrip(".")

def suggest_kaspi_category_query(title: str | None, characteristics: list[dict[str, str]]) -> str | None:
    chunks = [title or ""]
    for row in characteristics[:80]:
        chunks.append(str(row.get("name") or ""))
        chunks.append(str(row.get("value") or ""))
    text = " ".join(chunks).casefold().replace("ё", "е")
    supplement = any(token in text for token in (
        "бад", "витамин", "хлорофилл", "омега", "магний", "цинк", "коллаген",
        "пробиот", "минерал", "пищев", "добавк",
    ))
    if not supplement:
        return None
    liquid = any(token in text for token in (
        "жидк", "сироп", "раствор", "капли", "настой", "концентрат", "порош",
        "сыпуч", "гранул", " мл", "миллилитр",
    ))
    solid = any(token in text for token in (
        "капсул", "таблет", "драже", "пастил", "жеватель", "леденц",
    ))
    if liquid and not solid:
        return "Жидкие и сыпучие витамины и бад"
    if solid and not liquid:
        return "Твердые витамины и бад"
    return "витамины и бад"


def extract_characteristics(root: Any) -> list[dict[str, str]]:
    pairs: list[tuple[int, str, str]] = []
    seen: set[tuple[str, str]] = set()

    # Exact trusted facts first.  They get priority over generic widget rows.
    for row in extract_labeled_facts(root):
        name = str(row.get("name") or "").strip()
        value = str(row.get("value") or "").strip()
        if not name or not value:
            continue
        pair = (name.casefold().replace("ё", "е"), value.casefold().replace("ё", "е"))
        if pair not in seen:
            seen.add(pair)
            pairs.append((-2, name, value))

    for path, node in _walk(root):
        if not isinstance(node, dict):
            continue

        path_low = path.casefold()
        in_spec_area = any(token in path_low for token in (
            "character", "specification", "attribute", "property", "detail", "feature",
        ))

        # Preferred Ozon shape: {name/title: ..., value/values: ...}.
        name = None
        for key in ("name", "title", "label", "caption", "header"):
            raw_name = node.get(key)
            text = _clean_text(raw_name, 120) if isinstance(raw_name, str) else _rich_text(raw_name, 120)
            if text and _meaningful_spec_name(text):
                name = text
                break
        if name:
            value = None
            for key in ("values", "contentRS", "contentRs", "valueRs", "valueRS", "value", "text", "content"):
                if key in node:
                    value = _value_text(node.get(key))
                    if value:
                        break
            norm = name.casefold().replace("ё", "е").strip(" :")
            if value and 0 < len(value) <= 1200 and name.casefold() != value.casefold():
                key_pair = (norm, value.casefold().replace("ё", "е"))
                if key_pair not in seen:
                    seen.add(key_pair)
                    priority = 0 if in_spec_area else 4
                    pairs.append((priority, name, value))

        # Fallback for maps such as {"Страна производства": "США"}.
        # Exact trusted labels are accepted anywhere; arbitrary human keys only in spec areas.
        for key, raw in node.items():
            key_text = _clean_text(str(key), 100)
            canonical = _canonical_fact_label(key_text or "")
            if not canonical and not in_spec_area:
                continue
            if str(key).casefold() in {
                "id", "type", "name", "title", "label", "value", "values",
                "text", "content", "actiontype", "behavior", "color", "font",
                "key", "link", "limit",
            }:
                continue
            if not key_text or not _meaningful_spec_name(key_text):
                continue
            if not canonical and not re.search(r"[а-яА-ЯёЁ]", key_text) and " " not in key_text:
                continue
            value = _value_text(raw)
            if not value or len(value) > 1200:
                continue
            final_name = canonical or key_text
            key_pair = (final_name.casefold().replace("ё", "е"), value.casefold().replace("ё", "е"))
            if key_pair not in seen:
                seen.add(key_pair)
                pairs.append((1 if canonical else 2, final_name, value))

    pairs.sort(key=lambda x: (x[0], x[1].casefold()))
    return [{"name": name, "value": value} for _, name, value in pairs[:160]]


def _gallery_roots(root: Any) -> list[Any]:
    # Pick the primary product gallery only.  Ozon recommendation carousels also
    # carry productImages and were the source of unrelated photos in v0.5.1.
    candidates: list[tuple[int, int, Any]] = []
    blocked_path = ("recommend", "similar", "carousel", "tilegrid", "search", "seller", "also", "advert")
    order = 0
    for path, node in _walk(root):
        if not isinstance(node, dict):
            continue
        path_low = path.casefold()
        if any(token in path_low for token in blocked_path):
            continue
        for key, value in node.items():
            key_low = str(key).casefold()
            score = None
            if "gallery" in key_low:
                score = 0
            elif "productimages" in key_low or "product-images" in key_low:
                score = 4
            if score is None:
                continue
            candidates.append((score, order, _jsonish(value)))
            order += 1
    if not candidates:
        return []
    candidates.sort(key=lambda row: (row[0], row[1]))
    best_score = candidates[0][0]
    # Multiple values inside the same gallery widget are okay, but do not merge
    # weaker productImages roots once a real gallery was found.
    return [value for score, _, value in candidates if score == best_score][:3]


def _image_asset_key(url: str) -> str:
    try:
        path = urlsplit(url).path
    except Exception:
        return url
    name = path.rsplit("/", 1)[-1].casefold()
    stem = name.rsplit(".", 1)[0]
    return stem or name or path.casefold()


def _image_quality(url: str) -> tuple[int, int]:
    try:
        parts = [p for p in urlsplit(url).path.split("/") if p]
    except Exception:
        return (99, len(url))
    resize_penalty = sum(1 for p in parts if _RESIZE_SEGMENT_RE.match(p))
    query_penalty = 1 if urlsplit(url).query else 0
    return (resize_penalty * 10 + query_penalty, len(url))

def extract_images(root: Any) -> list[str]:
    # Main product gallery first. Recommendation widgets often contain dozens of unrelated images.
    gallery_nodes = _gallery_roots(root)
    search_roots = gallery_nodes or [root]
    best_by_asset: dict[str, tuple[tuple[int, int], str, int]] = {}
    order = 0
    for source in search_roots:
        for path, node in _walk(source):
            strings: list[str] = []
            if isinstance(node, str):
                strings.append(node)
            elif isinstance(node, dict):
                for key in ("url", "image", "imageUrl", "src", "original", "large"):
                    value = node.get(key)
                    if isinstance(value, str):
                        strings.append(value)
            for raw in strings:
                normalized = raw.replace("\\u002F", "/").replace("\\/", "/")
                for match in URL_RE.findall(normalized):
                    url = match.rstrip(",;)]}")
                    try:
                        parts = urlsplit(url)
                    except Exception:
                        continue
                    host = (parts.hostname or "").lower()
                    low = url.lower()
                    if not any(marker in host for marker in IMAGE_HOST_MARKERS):
                        continue
                    if "multimedia" not in low and not re.search(r"\.(?:jpe?g|png|webp)(?:$|\?)", low):
                        continue
                    if any(token in low for token in ("icon", "logo", "sprite", "avatar", "badge")):
                        continue
                    asset = _image_asset_key(url)
                    quality = _image_quality(url)
                    current = best_by_asset.get(asset)
                    if current is None:
                        best_by_asset[asset] = (quality, url, order)
                        order += 1
                    elif quality < current[0]:
                        best_by_asset[asset] = (quality, url, current[2])
    rows = sorted(best_by_asset.values(), key=lambda row: row[2])
    return [url for _, url, _ in rows[:20]]

def _parse_json_ld_from_html(text: str) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
    for raw in JSON_LD_RE.findall(text):
        try:
            value = json.loads(html.unescape(raw.strip()))
        except Exception:
            continue
        for _, node in _walk(value):
            if not isinstance(node, dict):
                continue
            type_value = node.get("@type")
            types = type_value if isinstance(type_value, list) else [type_value]
            if any(str(item).casefold() == "product" for item in types if item is not None):
                products.append(node)
    return products


def parse_ozon_response(content: bytes, content_type: str, final_url: str) -> dict[str, Any]:
    text = content.decode("utf-8", errors="replace")
    payload: Any = None
    parser = "html"
    if "json" in content_type.lower() or text.lstrip().startswith(("{", "[")):
        try:
            payload = json.loads(text)
            parser = "json"
        except Exception:
            payload = None

    if payload is not None:
        title = clean_product_title(_first_by_keys(payload, {"productName", "productTitle", "title"}, min_len=4, max_len=1024))
        brand = _first_by_keys(payload, {"brandName", "brand"}, min_len=1, max_len=256)
        description = _first_by_keys(payload, {"description", "annotation", "shortDescription"}, min_len=8, max_len=8000)
        characteristics = extract_characteristics(payload)
        characteristics = enrich_supplement_characteristics(title, characteristics)
        images = extract_images(payload)
    else:
        products = _parse_json_ld_from_html(text)
        product = products[0] if products else {}
        meta = {k.casefold(): html.unescape(v) for k, v in META_RE.findall(text)}
        title = clean_product_title(_clean_text(product.get("name"), 1024) or _clean_text(meta.get("og:title"), 1024))
        brand_value = product.get("brand")
        if isinstance(brand_value, dict):
            brand_value = brand_value.get("name")
        brand = _clean_text(brand_value, 256)
        description = _clean_text(product.get("description"), 8000) or _clean_text(meta.get("description"), 8000)
        characteristics = extract_characteristics(product)
        characteristics = enrich_supplement_characteristics(title, characteristics)
        images = extract_images(product)
        og_image = meta.get("og:image")
        if og_image and og_image.startswith("http") and og_image not in images:
            images.insert(0, og_image)

    bullet_description = build_semantic_description(characteristics, description, title=title, brand=brand)

    return {
        "ok": bool(title and images),
        "source_url": final_url,
        "parser": parser,
        "title": title,
        "brand": brand,
        "description_raw": description,
        "description": bullet_description,
        "characteristics": characteristics,
        "category_hint": suggest_kaspi_category_query(title, characteristics),
        "weight_kg": extract_logistics_weight_kg(characteristics),
        "images": images,
        "diagnostics": {
            "response_bytes": len(content),
            "content_type": content_type,
            "title_found": bool(title),
            "brand_found": bool(brand),
            "description_found": bool(description),
            "characteristics": len(characteristics),
            "images": len(images),
        },
    }
