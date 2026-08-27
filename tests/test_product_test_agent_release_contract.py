from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_product_test_agent_is_a_standalone_dedicated_runtime() -> None:
    source = (ROOT / "tools/product_test_agent.py").read_text(encoding="utf-8")
    assert 'VERSION = "1.0.2"' in source
    assert 'AGENT_KIND = "product_test"' in source
    assert "/api/product-test-agent/heartbeat" in source
    assert "/api/product-test-agent/claim" in source
    assert "discover_products" in source
    assert "validate_supplier_url" in source
    assert "create_linked_offer" in source
    assert "/api/fast-dumping-agent/claim" not in source
    assert "Local\\\\LEO-Product-Test-Agent-workspace-" in source


def test_product_test_agent_has_an_independent_windows_release() -> None:
    workflow = (
        ROOT / ".github/workflows/product-test-agent-release.yml"
    ).read_text(encoding="utf-8")
    assert "LEO-Product-Test-Agent.exe" in workflow
    assert "product-test-agent-latest" in workflow
    assert "tools/product_test_agent.py" in workflow
    assert "pyinstaller --noconfirm --clean --onefile --console" in workflow


def test_fast_agent_release_no_longer_owns_product_discovery_dependencies() -> None:
    workflow = (
        ROOT / ".github/workflows/kaspi-fast-dumping-agent-release.yml"
    ).read_text(encoding="utf-8")
    source = (ROOT / "tools/kaspi_fast_dumping_agent.py").read_text(encoding="utf-8")
    assert 'VERSION = "1.2.1"' in source
    assert "tools/product_discovery/**" not in workflow
    assert "tools/ozon_http/**" not in workflow
    assert "/api/product-test-agent/claim" not in source
