from pathlib import Path


STATIC = Path("backend/app/static")
CRM_PAGES = (
    "dashboard.html",
    "products.html",
    "product-detail.html",
    "orders.html",
    "revenue.html",
    "dumping.html",
    "suppliers.html",
    "monitoring.html",
)


def test_every_served_crm_page_loads_shared_theme_contract() -> None:
    for filename in CRM_PAGES:
        source = (STATIC / filename).read_text(encoding="utf-8")
        head = source.split("</head>", 1)[0]
        assert 'href="/static/responsive-ui.css?v=20260803-2"' in head
        assert 'id="light-theme-stylesheet"' in head
        assert 'href="/static/light-theme.css?v=20260803-2"' in head
        assert 'media="not all"' in head
        assert 'src="/static/theme.js?v=20260803-2"' in head
        assert head.rfind("light-theme.css") > head.rfind("dashboard.css")


def test_responsive_layer_applies_to_both_themes() -> None:
    source = (STATIC / "responsive-ui.css").read_text(encoding="utf-8")

    assert "@media (max-width: 1440px) and (min-width: 981px)" in source
    assert ".shell { grid-template-columns: 196px minmax(0, 1fr); }" in source
    assert ".topbar > .order-actions { flex-basis: 100%" in source
    assert ".order-header { grid-template-columns:" in source
    assert "main { min-width: 0; }" in source


def test_theme_toggle_defaults_to_blue_and_persists_light_choice() -> None:
    source = (STATIC / "theme.js").read_text(encoding="utf-8")

    assert 'const storageKey = "leo.crm.interface-theme"' in source
    assert 'const blueTheme = "blue"' in source
    assert 'const lightTheme = "light"' in source
    assert 'stylesheet.media = nextTheme === lightTheme ? "all" : "not all"' in source
    assert "localStorage.setItem(storageKey, nextTheme)" in source
    assert 'data-interface-theme-toggle' in source


def test_light_theme_remains_a_color_only_optional_layer() -> None:
    source = (STATIC / "light-theme.css").read_text(encoding="utf-8")

    assert "color-scheme: light" in source
    assert "background: var(--page)" in source
    assert "@media (max-width: 1440px)" not in source
