import json

from tools.product_test_new_card.parser import parse_ozon_response
from tools.ozon_http.session_profile import CurlProfile


def test_json_product_parser_extracts_fields_images_specs():
    payload = {
        "widgetStates": {
            "webProductHeading-1": json.dumps({"title": "Solgar Omega 3 950 mg"}),
            "webCharacteristics-1": json.dumps({
                "characteristics": [
                    {"name": "Бренд", "value": "Solgar"},
                    {"name": "Страна производства", "value": "США"},
                    {"name": "Количество капсул", "value": "100 шт"},
                ]
            }),
            "webGallery-1": json.dumps({
                "images": [
                    {"url": "https://ir.ozone.ru/s3/multimedia-a/test1.webp"},
                    {"url": "https://ir.ozone.ru/s3/multimedia-b/test2.webp"},
                ]
            }),
        },
        "brandName": "Solgar",
        "description": "Омега-3",
    }
    result = parse_ozon_response(json.dumps(payload, ensure_ascii=False).encode(), "application/json", "https://www.ozon.kz/product/x-123456/")
    assert result["title"] == "Solgar Omega 3 950 mg"
    assert result["brand"] == "Solgar"
    assert len(result["images"]) == 2
    names = {row["name"] for row in result["characteristics"]}
    assert "Страна производства" in names
    assert result["description"].startswith("-")


def test_html_jsonld_fallback():
    html = b'''<html><head><meta property="og:image" content="https://ir.ozone.ru/s3/multimedia-x/a.webp"></head><body>
    <script type="application/ld+json">{"@type":"Product","name":"Test Product","brand":{"name":"Brand X"},"description":"Long description","image":["https://ir.ozone.ru/s3/multimedia-x/a.webp"]}</script>
    </body></html>'''
    result = parse_ozon_response(html, "text/html", "https://www.ozon.kz/product/test-123456/")
    assert result["title"] == "Test Product"
    assert result["brand"] == "Brand X"
    assert result["images"]


def test_curl_profile_rewrites_composer_url_without_browser():
    raw = "curl 'https://www.ozon.kz/api/composer-api.bx/page/json/v2?url=%2Fsearch%2F%3Ftext%3Domega' -H 'User-Agent: Chrome' -H 'Cookie: a=b'"
    profile = CurlProfile.parse(raw)
    target = profile.rewritten_page_url("https://www.ozon.kz/product/omega-123456/")
    assert "composer-api" in target
    assert "product%2Fomega-123456" in target or "product%2Fomega-123456%2F" in target
    assert profile.request_headers_for_page("https://www.ozon.kz/product/omega-123456/")["Cookie"] == "a=b"


def test_curl_profile_preserves_exact_product_query_when_rewriting_page_url():
    raw = "curl 'https://www.ozon.kz/api/composer-api.bx/page/json/v2?url=%2Fsearch%2F%3Ftext%3Domega' -H 'User-Agent: Chrome'"
    profile = CurlProfile.parse(raw)

    target = profile.rewritten_page_url(
        "https://www.ozon.kz/product/omega-123456/?at=selected-offer-token&sh=share-token"
    )

    assert "at%3Dselected-offer-token" in target
    assert "sh%3Dshare-token" in target


def test_parser_filters_ozon_widget_metadata_cleans_title_and_deduplicates_gallery():
    payload = {
        "widgetStates": {
            "webProductHeading-1": json.dumps({
                "title": "Жидкий хлорофилл концентрат 100 пищевой с мятой 500 мл Miopharm купить на OZON по низкой цене в Казахстане (4831073519)"
            }),
            "webCharacteristics-1": json.dumps({
                "actionType": "click",
                "behavior": "BEHAVIOR_TYPE_SCROLL_TO_WIDGET",
                "color": "textPrimary",
                "key": "technical-key",
                "characteristics": [
                    {"name": "Форма выпуска", "value": "Жидкость"},
                    {"name": "Страна производства", "value": "Россия"},
                    {"name": "Количество штук в упаковке", "value": "1"},
                ],
            }),
            "webGallery-1": json.dumps({
                "images": [
                    {"url": "https://ir-3.ozone.ru/s3/multimedia-1-h/wc140/12069705905.jpg"},
                    {"url": "https://ir-3.ozone.ru/s3/multimedia-1-h/c50/12069705905.jpg"},
                    {"url": "https://ir-3.ozone.ru/s3/multimedia-1-h/12069705905.jpg"},
                    {"url": "https://ir-3.ozone.ru/s3/multimedia-1-k/13438942148.jpg"},
                ]
            }),
            "webRecommendations-1": json.dumps({
                "images": [{"url": "https://ir-3.ozone.ru/s3/multimedia-x/99999999999.jpg"}]
            }),
        },
        "brandName": "Miopharm",
    }
    result = parse_ozon_response(
        json.dumps(payload, ensure_ascii=False).encode(),
        "application/json",
        "https://www.ozon.kz/product/x-4831073519/",
    )
    assert result["title"] == "Жидкий хлорофилл концентрат 100 пищевой с мятой 500 мл Miopharm"
    assert result["category_hint"] == "Жидкие и сыпучие витамины и бад"
    assert result["images"] == [
        "https://ir-3.ozone.ru/s3/multimedia-1-h/12069705905.jpg",
        "https://ir-3.ozone.ru/s3/multimedia-1-k/13438942148.jpg",
    ]
    names = {row["name"] for row in result["characteristics"]}
    assert {"Форма выпуска", "Страна производства", "Количество штук в упаковке"}.issubset(names)
    assert "Количество упаковок" in names
    assert "actionType" not in result["description"]
    assert "behavior" not in result["description"]


def test_parser_extracts_real_baad_facts_from_ozon_text_widget_and_builds_clean_description():
    text_blob = r'''### Дозировка
Содержание в суточной порции (5 мл): хлорофилл - 15 мг, медь - 0,75 мг.

### Состав
Форма выпуска: флаконы, бутылки по 100-500 мл
Состав: вода очищенная структурированная, растительный глицерин, медные комплексы хлорофиллинов натрия, эфирное масло мяты перечной.

### Способ применения
Рекомендации по применению: взрослым по чайной ложке (5 мл), разведенной в 150–200 мл воды 1 раз в день за 30 минут до еды.
Продолжительность приема: 1 месяц.
Противопоказания: Индивидуальная непереносимость компонентов, беременность, кормление грудью.
Условия хранения: хранить в сухом, защищенном от прямых солнечных лучей месте.
Срок годности - 2 года от даты изготовления.

### Показания
Область применения: для реализации населению в качестве биологически активной добавки к пище – источника хлорофилла и меди.

Характеристики
Артикул
4831073519
Тип
Витамины
Основной компонент
svg
Хлорофилл
Страна-изготовитель
Россия
Номер СГР
AM.01.01.01.003.R.001012.07.26
Объем, мл
500
Целевая аудитория
Взрослая
Вкусовой акцент (вкус)
Мята
Форма выпуска продукта
Жидкость
Количество в упаковке, шт
300
Противопоказания БАД
БАД. НЕ ЯВЛЯЕТСЯ ЛЕКАРСТВЕННЫМ СРЕДСТВОМ
Для детей
Нет
Минимальный возраст от
18 лет
Направление витаминов:
Для восстановления микрофлоры кишечника, Для тонуса и укрепления организма
'''
    payload = {
        "widgetStates": {
            "webProductHeading-1": json.dumps({"title": "Жидкий хлорофилл 500 мл Miopharm купить на OZON по низкой цене в Казахстане (4831073519)"}),
            "webDescription-1": json.dumps({"text": text_blob}),
            "webGallery-1": json.dumps({"images": [{"url": "https://ir-3.ozone.ru/s3/multimedia-1-h/12069705905.jpg"}]}),
        },
        "brandName": "Miopharm",
        "description": "OZON по выгодным ценам в Казахстане! Интернет-магазин OZON Казахстан",
    }
    result = parse_ozon_response(
        json.dumps(payload, ensure_ascii=False).encode(),
        "application/json",
        "https://www.ozon.kz/product/x-4831073519/",
    )
    facts = {row["name"]: row["value"] for row in result["characteristics"]}
    assert facts["Основной компонент"] == "Хлорофилл"
    assert facts["Номер СГР"] == "AM.01.01.01.003.R.001012.07.26"
    assert facts["Объем, мл"] == "500"
    assert facts["Рекомендуемый возраст"] == "18 лет"
    assert facts["Для кого"] == "универсальные"
    assert facts["Регистрационный статус"] == "не является лекарственным средством"
    assert facts["Количество штук в упаковке"] == "1"
    assert facts["Количество упаковок"] == "1"
    assert facts["Название препарата"] == "Хлорофилл"
    assert facts["Назначение"].startswith("Для восстановления микрофлоры кишечника")
    assert "Состав: вода очищенная" in result["description"]
    assert "Рекомендации по применению:" in result["description"]
    assert "msapplication" not in result["description"].casefold()
    assert "OZON по выгодным ценам" not in result["description"]


def test_gallery_ignores_recommendation_product_images_when_main_gallery_exists():
    payload = {
        "widgetStates": {
            "webGallery-1": json.dumps({"images": [
                {"url": "https://ir-3.ozone.ru/s3/multimedia-main/111.jpg"},
                {"url": "https://ir-3.ozone.ru/s3/multimedia-main/222.jpg"},
            ]}),
            "webRecommendations-1": json.dumps({"productImages": [
                {"url": "https://ir-3.ozone.ru/s3/multimedia-other/999.jpg"}
            ]}),
            "webProductHeading-1": json.dumps({"title": "Хлорофилл 500 мл"}),
        },
        "brandName": "Miopharm",
    }
    result = parse_ozon_response(
        json.dumps(payload, ensure_ascii=False).encode(),
        "application/json",
        "https://www.ozon.kz/product/x-4831073519/",
    )
    assert result["images"] == [
        "https://ir-3.ozone.ru/s3/multimedia-main/111.jpg",
        "https://ir-3.ozone.ru/s3/multimedia-main/222.jpg",
    ]


def test_parser_standardizes_solid_omega_bad_characteristics_and_weight():
    payload = {
        "widgetStates": {
            "webProductHeading-1": json.dumps({"title": "Uniforce Omega 3 120 капсул"}),
            "webCharacteristics-1": json.dumps({
                "characteristics": [
                    {"name": "Артикул", "value": "170524351"},
                    {"name": "Тип", "value": "БАД жирные кислоты"},
                    {"name": "Основной компонент", "value": "Омега 3"},
                    {"name": "Страна-изготовитель", "value": "США"},
                    {"name": "Бренд", "value": "Uniforce"},
                    {"name": "Номер СГР", "value": "RU.77.99.88.003.E.001953.05.18"},
                    {"name": "Вес товара, г", "value": "212"},
                    {"name": "Целевая аудитория", "value": "Взрослая"},
                    {"name": "Направление БАД", "value": "Против сердечно-сосудистых нарушений"},
                    {"name": "Форма выпуска продукта", "value": "Капсулы"},
                    {"name": "Количество в упаковке, шт", "value": "120"},
                    {"name": "Противопоказания БАД", "value": "БАД. НЕ ЯВЛЯЕТСЯ ЛЕКАРСТВЕННЫМ СРЕДСТВОМ"},
                    {"name": "Срок годности в днях", "value": "1095"},
                    {"name": "Для детей", "value": "Нет"},
                ]
            }),
            "webGallery-1": json.dumps({"images": [{"url": "https://ir-3.ozone.ru/s3/multimedia-main/omega.jpg"}]}),
        },
        "brandName": "Uniforce",
    }
    result = parse_ozon_response(
        json.dumps(payload, ensure_ascii=False).encode(),
        "application/json",
        "https://www.ozon.kz/product/uniforce-omega-3-170524351/",
    )
    facts = {row["name"]: row["value"] for row in result["characteristics"]}
    assert result["category_hint"] == "Твердые витамины и бад"
    assert result["weight_kg"] == "0.212"
    assert facts["Название препарата"] == "Омега 3"
    assert facts["Назначение"] == "Против сердечно-сосудистых нарушений"
    assert facts["Область применения"] == "Против сердечно-сосудистых нарушений"
    assert facts["Рекомендуемый возраст"] == "18 лет"
    assert facts["Для кого"] == "универсальные"
    assert facts["Форма выпуска"] == "Капсулы"
    assert facts["Количество штук в упаковке"] == "120"
    assert facts["Количество"] == "120"
    assert facts["Количество упаковок"] == "1"
    assert facts["Страна производитель"] == "США"
    assert facts["Регистрационный статус"] == "не является лекарственным средством"


def test_parser_real_ozon_rich_text_characteristics_shape_for_uniforce_omega():
    """Regression for current Ozon webShortCharacteristics rich-text rows.

    Real composer responses commonly use title.textRs + values/contentRS instead
    of the old {name,value} shape.  These are the fields visible on the Ozon
    Omega-3 card that were blank in lab v1.1.0.
    """
    def rs_title(text):
        return {"textRs": [{"content": text}]}

    def rs_value(text):
        return [{"content": text}]

    rows = [
        {"title": rs_title("Артикул"), "values": rs_value("170524351")},
        {"title": rs_title("Тип"), "values": rs_value("БАД жирные кислоты")},
        {"title": rs_title("Основной компонент"), "values": rs_value("Омега 3")},
        {"title": rs_title("Страна-изготовитель"), "values": rs_value("США")},
        {"title": rs_title("Бренд"), "values": rs_value("Uniforce")},
        {"title": rs_title("Номер СГР"), "values": rs_value("RU.77.99.88.003.E.001953.05.18")},
        {"title": rs_title("Вес товара, г"), "values": rs_value("212")},
        {"title": rs_title("Целевая аудитория"), "values": rs_value("Взрослая")},
        {"title": rs_title("Направление БАД"), "values": rs_value("Против сердечно-сосудистых нарушений")},
        {"title": rs_title("Форма выпуска продукта"), "values": rs_value("Капсулы")},
        {"title": rs_title("Количество в упаковке, шт"), "values": rs_value("120")},
        {"title": rs_title("Противопоказания БАД"), "values": rs_value("БАД. НЕ ЯВЛЯЕТСЯ ЛЕКАРСТВЕННЫМ СРЕДСТВОМ")},
        {"title": rs_title("Срок годности в днях"), "values": rs_value("1095")},
        {"title": rs_title("Для детей"), "values": rs_value("Нет")},
    ]
    payload = {
        "widgetStates": {
            "webProductHeading-1": json.dumps({"title": "Omega-3 Uniforce 1000 mg 120 капс."}, ensure_ascii=False),
            "webShortCharacteristics-1": json.dumps({"characteristics": rows}, ensure_ascii=False),
            "webGallery-1": json.dumps({"images": [{"url": "https://ir-3.ozone.ru/s3/multimedia-main/omega.jpg"}]}, ensure_ascii=False),
        },
        "brandName": "Uniforce",
    }
    result = parse_ozon_response(
        json.dumps(payload, ensure_ascii=False).encode(),
        "application/json",
        "https://www.ozon.kz/product/omega-3-uniforce-omega-3-1000-mg-120-kaps-170524351/",
    )
    facts = {row["name"]: row["value"] for row in result["characteristics"]}
    assert facts["Основной компонент"] == "Омега 3"
    assert facts["Назначение"] == "Против сердечно-сосудистых нарушений"
    assert facts["Область применения"] == "Против сердечно-сосудистых нарушений"
    assert facts["Рекомендуемый возраст"] == "18 лет"
    assert facts["Для кого"] == "универсальные"
    assert facts["Форма выпуска"] == "Капсулы"
    assert facts["Количество штук в упаковке"] == "120"
    assert facts["Количество"] == "120"
    assert facts["Количество упаковок"] == "1"
    assert facts["Страна производитель"] == "США"
    assert result["weight_kg"] == "0.212"


def test_title_backstop_recovers_solid_form_and_amount_when_widget_omits_them():
    payload = {
        "widgetStates": {
            "webProductHeading-1": json.dumps({"title": "Omega-3 Uniforce 1000 mg 120 капс."}, ensure_ascii=False),
            "webShortCharacteristics-1": json.dumps({
                "characteristics": [
                    {"title": {"textRs": [{"content": "Основной компонент"}]}, "values": [{"content": "Омега 3"}]},
                    {"title": {"textRs": [{"content": "Страна-изготовитель"}]}, "values": [{"content": "США"}]},
                    {"title": {"textRs": [{"content": "Для детей"}]}, "values": [{"content": "Нет"}]},
                ]
            }, ensure_ascii=False),
            "webGallery-1": json.dumps({"images": [{"url": "https://ir-3.ozone.ru/s3/multimedia-main/omega.jpg"}]}, ensure_ascii=False),
        },
        "brandName": "Uniforce",
    }
    result = parse_ozon_response(
        json.dumps(payload, ensure_ascii=False).encode(),
        "application/json",
        "https://www.ozon.kz/product/omega-3-170524351/",
    )
    facts = {row["name"]: row["value"] for row in result["characteristics"]}
    assert facts["Форма выпуска"] == "Капсулы"
    assert facts["Количество"] == "120"
    assert facts["Количество упаковок"] == "1"


def test_semantic_description_reaches_kaspi_minimum_for_sparse_omega_facts():
    from tools.product_test_new_card.parser import build_semantic_description

    rows = [
        {"name": "Основной компонент", "value": "Омега 3"},
        {"name": "Количество штук в упаковке", "value": "120"},
        {"name": "Форма выпуска", "value": "Капсулы"},
        {"name": "Страна производитель", "value": "США"},
        {"name": "Номер СГР", "value": "RU.77.99.88.003.E.001953.05.18"},
    ]
    text = build_semantic_description(
        rows,
        None,
        title="Uniforce Omega 3 для взрослых 120 капсул",
        brand="Uniforce",
    )
    assert len(text) >= 100
    assert len(text) <= 1024
    assert "Омега 3" in text
    assert "Капсулы" in text
    assert "RU.77.99.88.003.E.001953.05.18" in text
