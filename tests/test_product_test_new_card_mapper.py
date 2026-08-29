from tools.product_test_new_card.mapper import build_payload, map_characteristics, similarity, validate_payload


def test_similarity_matches_common_ru_fields():
    assert similarity("Страна производства", "Страна-изготовитель") > 0.45
    assert similarity("Цвет товара", "Цвет") > 0.8


def test_map_characteristics_and_allowed_values():
    source = [
        {"name": "Цвет товара", "value": "Черный"},
        {"name": "Страна производства", "value": "США"},
    ]
    attrs = [
        {"code": "A*color", "title": "Цвет", "required": True},
        {"code": "A*country", "title": "Страна-изготовитель", "required": False},
    ]
    values = {"A*color": [{"code": "черный", "name": "черный"}]}
    mapped = map_characteristics(source, attrs, values)
    assert mapped[0]["value"] == "черный"
    assert mapped[0]["source_name"] == "Цвет товара"
    assert mapped[1]["source_name"] == "Страна производства"


def test_payload_shape_uses_kaspi_images_url_objects():
    mapped = [{"code": "A*color", "title": "Цвет", "required": True, "value": "черный"}]
    payload = build_payload(
        sku="OZON-123",
        title="Тестовый товар",
        brand="Brand",
        category="Master - Test",
        description="- Цвет: черный",
        attributes=mapped,
        images=["https://ir.ozone.ru/test.webp"],
    )
    assert payload["images"] == [{"url": "https://ir.ozone.ru/test.webp"}]
    assert payload["attributes"] == [{"code": "A*color", "value": "черный"}]
    assert validate_payload(payload, mapped) == []


def test_multivalued_and_typed_values_are_emitted_correctly():
    attrs = [
        {"code": "A*colors", "title": "Colors", "required": False, "type": "enum", "multi_valued": True, "value": "red; blue"},
        {"code": "A*count", "title": "Count", "required": False, "type": "number", "multi_valued": False, "value": "24"},
        {"code": "A*flag", "title": "Flag", "required": False, "type": "boolean", "multi_valued": False, "value": "да"},
    ]
    payload = build_payload(
        sku="S1", title="T", brand="B", category="Master - Test", description="D",
        attributes=attrs, images=["https://ir.ozone.ru/x.webp"],
    )
    values = {row["code"]: row["value"] for row in payload["attributes"]}
    assert values["A*colors"] == ["red", "blue"]
    assert values["A*count"] == 24
    assert values["A*flag"] is True


def test_low_confidence_enum_is_left_empty_instead_of_sending_invalid_value():
    source = [{"name": "Форма выпуска", "value": "совсем другое значение"}]
    attrs = [{"code": "A*form", "title": "Форма выпуска", "required": True, "type": "enum"}]
    values = {"A*form": [{"code": "caps", "name": "Капсулы"}, {"code": "liquid", "name": "Жидкость"}]}
    mapped = map_characteristics(source, attrs, values)
    assert mapped[0]["value"] == ""
    assert mapped[0]["source_value"] == "совсем другое значение"


def test_supplement_synonyms_match_kaspi_required_fields():
    source = [
        {"name": "Количество капсул", "value": "90"},
        {"name": "Основной ингредиент", "value": "Цинк"},
        {"name": "СГР", "value": "KZ.00.00.01.003.R.000001"},
    ]
    attrs = [
        {"code": "A*qty", "title": "Количество штук в упаковке", "required": True},
        {"code": "A*component", "title": "Основной компонент", "required": True},
        {"code": "A*sgr", "title": "Номер СГР", "required": True},
    ]
    mapped = map_characteristics(source, attrs, {})
    assert mapped[0]["source_name"] == "Количество капсул"
    assert mapped[1]["source_name"] == "Основной ингредиент"
    assert mapped[2]["source_name"] == "СГР"


def test_baad_required_fields_are_filled_from_exact_ozon_sources_not_fuzzy_random_rows():
    source = [
        {"name": "Название препарата", "value": "Хлорофилл"},
        {"name": "Назначение", "value": "Для восстановления микрофлоры кишечника, Для тонуса и укрепления организма"},
        {"name": "Область применения", "value": "Для пищеварительной системы"},
        {"name": "Основной компонент", "value": "Хлорофилл"},
        {"name": "Регистрационный статус", "value": "не является лекарственным средством"},
        {"name": "Номер СГР", "value": "AM.01.01.01.003.R.001012.07.26"},
        {"name": "Рекомендуемый возраст", "value": "18 лет"},
        {"name": "Для кого", "value": "универсальные"},
        {"name": "Объем жидкости", "value": "500"},
        {"name": "Форма выпуска", "value": "Жидкость"},
        {"name": "Количество упаковок", "value": "1"},
        {"name": "Страна производитель", "value": "Россия"},
        # Deliberate noise that old fuzzy mapping could accidentally choose.
        {"name": "Для похудения", "value": "Корица"},
    ]
    attrs = [
        {"code": "Vitamins*Drug name", "title": "Vitamins*Drug name", "required": True, "type": "string"},
        {"code": "Vitamins*Purpose", "title": "Vitamins*Purpose", "required": True, "type": "enum", "multi_valued": True},
        {"code": "Dietary supplements*Range of applications", "title": "Dietary supplements*Range of applications", "required": True, "type": "enum", "multi_valued": True},
        {"code": "Dietary supplements*Main component", "title": "Dietary supplements*Main component", "required": True, "type": "enum", "multi_valued": True},
        {"code": "Vitamins*Registration status", "title": "Vitamins*Registration status", "required": True, "type": "enum"},
        {"code": "Dietary supplements*SGR number", "title": "Dietary supplements*SGR number", "required": True, "type": "string"},
        {"code": "Dietary supplements*Recommended age", "title": "Dietary supplements*Recommended age", "required": True, "type": "enum"},
        {"code": "Vitamins*Gender", "title": "Vitamins*Gender", "required": True, "type": "enum"},
        {"code": "Vitamins*Liquid volume", "title": "Vitamins*Liquid volume", "required": True, "type": "number"},
        {"code": "Vitamins*Dosage form", "title": "Vitamins*Dosage form", "required": True, "type": "enum"},
        {"code": "Vitamins*Number of packages", "title": "Vitamins*Number of packages", "required": True, "type": "number"},
        {"code": "Pharmacy*Country", "title": "Pharmacy*Country", "required": True, "type": "enum"},
    ]
    values = {
        "Vitamins*Purpose": [
            {"code": "gut", "name": "Для восстановления микрофлоры кишечника"},
            {"code": "tone", "name": "Для тонуса и укрепления организма"},
        ],
        "Dietary supplements*Range of applications": [{"code": "digest", "name": "Для пищеварительной системы"}],
        "Dietary supplements*Main component": [{"code": "chlorophyll", "name": "Хлорофилл"}],
        "Vitamins*Registration status": [{"code": "not_drug", "name": "не является лекарственным средством"}],
        "Dietary supplements*Recommended age": [{"code": "18plus", "name": "от 18 лет"}, {"code": "16plus", "name": "от 16 лет"}],
        "Vitamins*Gender": [{"code": "unisex", "name": "универсальные"}],
        "Vitamins*Dosage form": [{"code": "liquid", "name": "жидкость"}],
        "Pharmacy*Country": [{"code": "ru", "name": "Россия"}],
    }
    mapped = map_characteristics(source, attrs, values)
    by_code = {row["code"]: row for row in mapped}
    assert by_code["Vitamins*Drug name"]["value"] == "Хлорофилл"
    assert by_code["Vitamins*Drug name"]["source_name"] == "Название препарата"
    assert by_code["Dietary supplements*Main component"]["value"] == "chlorophyll"
    assert by_code["Dietary supplements*SGR number"]["value"] == "AM.01.01.01.003.R.001012.07.26"
    assert by_code["Dietary supplements*Recommended age"]["value"] == "18plus"
    assert by_code["Vitamins*Liquid volume"]["value"] == "500"
    assert by_code["Vitamins*Number of packages"]["value"] == "1"
    assert by_code["Pharmacy*Country"]["value"] == "ru"
    assert by_code["Vitamins*Purpose"]["value"] == "gut; tone"
    assert by_code["Dietary supplements*Range of applications"]["value"] == "digest"


def test_baad_range_of_applications_maps_microflora_to_kaspi_digestive_enum():
    source = [
        {
            "name": "Направление витаминов",
            "value": "Для восстановления микрофлоры кишечника, Для тонуса и укрепления организма",
        },
    ]
    attrs = [
        {
            "code": "Dietary supplements*Range of applications",
            "title": "Dietary supplements*Range of applications",
            "required": True,
            "type": "enum",
            "multi_valued": True,
        },
    ]
    values = {
        "Dietary supplements*Range of applications": [
            {"code": "пищеварение", "name": "пищеварение"},
            {"code": "сердце и кровообращение", "name": "сердце и кровообращение"},
        ],
    }
    mapped = map_characteristics(source, attrs, values)
    assert mapped[0]["value"] == "пищеварение"
    assert mapped[0]["value_score"] >= 0.9


def test_baad_purpose_does_not_confuse_tone_with_cleansing():
    source = [
        {
            "name": "Направление витаминов",
            "value": "Для восстановления микрофлоры кишечника, Для тонуса и укрепления организма",
        },
    ]
    attrs = [
        {
            "code": "Vitamins*Purpose",
            "title": "Vitamins*Purpose",
            "required": True,
            "type": "enum",
            "multi_valued": True,
        },
    ]
    values = {
        "Vitamins*Purpose": [
            {"code": "восстановление микрофлоры кишечника", "name": "восстановление микрофлоры кишечника"},
            {"code": "для очищения организма", "name": "для очищения организма"},
            {"code": "для тонуса и укрепления организма", "name": "для тонуса и укрепления организма"},
        ],
    }
    mapped = map_characteristics(source, attrs, values)
    assert mapped[0]["value"] == "восстановление микрофлоры кишечника; для тонуса и укрепления организма"


def test_legal_baad_disclaimer_is_not_used_as_contraindication():
    source = [
        {"name": "Противопоказания БАД", "value": "БАД. НЕ ЯВЛЯЕТСЯ ЛЕКАРСТВЕННЫМ СРЕДСТВОМ"},
    ]
    attrs = [
        {
            "code": "Vitamins*Contraindications",
            "title": "Vitamins*Contraindications",
            "required": False,
            "type": "string",
            "multi_valued": False,
        },
    ]
    mapped = map_characteristics(source, attrs, {})
    assert mapped[0]["value"] == ""


def test_number_field_strips_unit_suffix_on_payload_build():
    attrs = [{"code": "Vitamins*Liquid volume", "title": "Volume", "required": True, "type": "number", "value": "500 мл"}]
    payload = build_payload(
        sku="S1", title="T", brand="B", category="Master - Vitamins", description="D",
        attributes=attrs, images=["https://ir.ozone.ru/x.webp"],
    )
    assert payload["attributes"][0]["value"] == 500


def test_solid_omega_bad_required_fields_fill_from_standardized_characteristics():
    source = [
        {"name": "Название препарата", "value": "Омега 3"},
        {"name": "Назначение", "value": "Против сердечно-сосудистых нарушений"},
        {"name": "Область применения", "value": "Против сердечно-сосудистых нарушений"},
        {"name": "Основной компонент", "value": "Омега 3"},
        {"name": "Для кого", "value": "универсальные"},
        {"name": "Регистрационный статус", "value": "не является лекарственным средством"},
        {"name": "Номер СГР", "value": "RU.77.99.88.003.E.001953.05.18"},
        {"name": "Рекомендуемый возраст", "value": "18 лет"},
        {"name": "Количество", "value": "120"},
        {"name": "Количество штук в упаковке", "value": "120"},
        {"name": "Форма выпуска", "value": "Капсулы"},
        {"name": "Количество упаковок", "value": "1"},
        {"name": "Страна производитель", "value": "США"},
    ]
    attrs = [
        {"code": "Vitamins*Drug name", "title": "Vitamins*Drug name", "required": True, "type": "string"},
        {"code": "Vitamins*Purpose", "title": "Vitamins*Purpose", "required": True, "type": "enum", "multi_valued": True},
        {"code": "Dietary supplements*Range of applications", "title": "Dietary supplements*Range of applications", "required": True, "type": "enum", "multi_valued": True},
        {"code": "Dietary supplements*Main component", "title": "Dietary supplements*Main component", "required": True, "type": "enum", "multi_valued": True},
        {"code": "Vitamins*Gender", "title": "Vitamins*Gender", "required": True, "type": "enum"},
        {"code": "Vitamins*Registration status", "title": "Vitamins*Registration status", "required": True, "type": "enum"},
        {"code": "Dietary supplements*SGR number", "title": "Dietary supplements*SGR number", "required": True, "type": "string"},
        {"code": "Dietary supplements*Recommended age", "title": "Dietary supplements*Recommended age", "required": True, "type": "enum"},
        {"code": "Dietary supplements*Amount", "title": "Dietary supplements*Amount", "required": True, "type": "number"},
        {"code": "Dietary supplements*Release form", "title": "Dietary supplements*Release form", "required": True, "type": "enum"},
        {"code": "Dietary supplements*Number of packages", "title": "Dietary supplements*Number of packages", "required": True, "type": "number"},
        {"code": "Pharmacy*Country", "title": "Pharmacy*Country", "required": True, "type": "enum"},
    ]
    values = {
        "Vitamins*Purpose": [
            {"code": "cardio", "name": "для сердца и сосудов"},
            {"code": "digest", "name": "для пищеварения"},
        ],
        "Dietary supplements*Range of applications": [
            {"code": "heart", "name": "сердце и кровообращение"},
            {"code": "digest", "name": "пищеварение"},
        ],
        "Dietary supplements*Main component": [{"code": "omega3", "name": "Омега 3"}],
        "Vitamins*Gender": [{"code": "unisex", "name": "универсальные"}],
        "Vitamins*Registration status": [{"code": "not_drug", "name": "не является лекарственным средством"}],
        "Dietary supplements*Recommended age": [
            {"code": "18plus", "name": "взрослым (старше 18 лет)"},
            {"code": "16plus", "name": "от 16 лет"},
        ],
        "Dietary supplements*Release form": [
            {"code": "caps", "name": "капсулы"},
            {"code": "tabs", "name": "таблетки"},
        ],
        "Pharmacy*Country": [{"code": "us", "name": "США"}],
    }
    mapped = map_characteristics(source, attrs, values)
    by_code = {row["code"]: row for row in mapped}
    assert by_code["Vitamins*Drug name"]["value"] == "Омега 3"
    assert by_code["Vitamins*Purpose"]["value"] == "cardio"
    assert by_code["Dietary supplements*Range of applications"]["value"] == "heart"
    assert by_code["Dietary supplements*Main component"]["value"] == "omega3"
    assert by_code["Vitamins*Gender"]["value"] == "unisex"
    assert by_code["Vitamins*Registration status"]["value"] == "not_drug"
    assert by_code["Dietary supplements*SGR number"]["value"] == "RU.77.99.88.003.E.001953.05.18"
    assert by_code["Dietary supplements*Recommended age"]["value"] == "18plus"
    assert by_code["Dietary supplements*Amount"]["value"] == "120"
    assert by_code["Dietary supplements*Release form"]["value"] == "caps"
    assert by_code["Dietary supplements*Number of packages"]["value"] == "1"
    assert by_code["Pharmacy*Country"]["value"] == "us"


def test_short_description_is_extended_to_kaspi_minimum_using_real_facts():
    attrs = [
        {
            "code": "Dietary supplements*Main component",
            "title": "Dietary supplements*Main component",
            "required": True,
            "type": "enum",
            "value": "omega3",
            "source_name": "Основной компонент",
            "source_value": "Омега 3",
        },
        {
            "code": "Dietary supplements*Amount",
            "title": "Dietary supplements*Amount",
            "required": True,
            "type": "number",
            "value": "120",
            "source_name": "Количество в упаковке, шт",
            "source_value": "120",
        },
        {
            "code": "Dietary supplements*Release form",
            "title": "Dietary supplements*Release form",
            "required": True,
            "type": "enum",
            "value": "caps",
            "source_name": "Форма выпуска продукта",
            "source_value": "Капсулы",
        },
        {
            "code": "Pharmacy*Country",
            "title": "Pharmacy*Country",
            "required": True,
            "type": "enum",
            "value": "us",
            "source_name": "Страна-изготовитель",
            "source_value": "США",
        },
    ]
    payload = build_payload(
        sku="170524351",
        title="Uniforce Omega 3 для взрослых 120 капсул",
        brand="Uniforce",
        category="Master - Vitamins",
        description="Омега 3",
        attributes=attrs,
        images=["https://ir.ozone.ru/x.webp"],
    )
    assert len(payload["description"]) >= 100
    assert len(payload["description"]) <= 1024
    assert "Омега 3" in payload["description"]
    assert "Капсулы" in payload["description"]
    assert validate_payload(payload, attrs) == []
