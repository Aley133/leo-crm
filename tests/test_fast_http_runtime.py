from __future__ import annotations

import ssl
from pathlib import Path

import certifi
import httpx

from tools import kaspi_fast_http_runtime as runtime


def test_ssl_context_survives_deleted_onefile_ca_bundle(
    monkeypatch,
    tmp_path,
) -> None:
    extracted_bundle = tmp_path / "cacert.pem"
    extracted_bundle.write_bytes(Path(certifi.where()).read_bytes())
    monkeypatch.setattr(runtime, "_bundled_ca_file", lambda: extracted_bundle)

    context = runtime.build_kaspi_ssl_context()
    extracted_bundle.unlink()
    monkeypatch.setenv("SSL_CERT_FILE", str(extracted_bundle))

    transport = httpx.MockTransport(lambda _request: httpx.Response(200))
    with httpx.Client(
        verify=context,
        trust_env=False,
        transport=transport,
    ) as client:
        assert client.get("https://kaspi.kz/").status_code == 200


def test_runtime_options_ignore_missing_environment_ca_paths(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "missing-directory"))

    context = runtime.build_kaspi_ssl_context()
    options = runtime.httpx_runtime_options()

    assert isinstance(context, ssl.SSLContext)
    assert isinstance(options["verify"], ssl.SSLContext)
    assert options["trust_env"] is False


def test_all_fast_kaspi_clients_use_the_stable_tls_runtime() -> None:
    root = Path(__file__).resolve().parents[1]
    scanner = (root / "tools/kaspi_fast_dumping_scanner.py").read_text(
        encoding="utf-8"
    )
    session = (root / "tools/kaspi_fast_dumping_session.py").read_text(
        encoding="utf-8"
    )
    auth = (root / "tools/kaspi_fast_dumping_http_auth.py").read_text(
        encoding="utf-8"
    )
    offer = (root / "tools/kaspi_fast_offer_runtime.py").read_text(
        encoding="utf-8"
    )

    assert scanner.count("**httpx_runtime_options()") == 2
    assert session.count("**httpx_runtime_options()") == 3
    assert auth.count("**httpx_runtime_options()") == 1
    assert offer.count("**httpx_runtime_options()") == 1
