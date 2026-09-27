from __future__ import annotations

import os
import ssl
from pathlib import Path
from typing import Any


def _bundled_ca_file() -> Path | None:
    try:
        import certifi

        candidate = Path(certifi.where())
    except (ImportError, OSError, TypeError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def _system_ssl_context_without_environment_paths() -> ssl.SSLContext:
    """Build from the OS store without inheriting stale CA paths."""

    names = ("SSL_CERT_FILE", "SSL_CERT_DIR")
    saved = {name: os.environ.pop(name) for name in names if name in os.environ}
    try:
        return ssl.create_default_context()
    finally:
        os.environ.update(saved)


def build_kaspi_ssl_context() -> ssl.SSLContext:
    """Load CA certificates into memory once, before onefile TEMP can change.

    PyInstaller onefile extracts certifi's bundle below ``%TEMP%\\_MEI...``.
    Windows cleanup tools may remove that unlocked PEM while the long-running
    Agent remains alive. A new httpx client would then fail with
    ``FileNotFoundError``. Loading the bundle into an SSLContext during module
    import makes every later request independent from the extracted file.
    """

    bundled = _bundled_ca_file()
    if bundled is not None:
        try:
            context = ssl.create_default_context(cafile=str(bundled))
        except OSError:
            context = _system_ssl_context_without_environment_paths()
    else:
        context = _system_ssl_context_without_environment_paths()

    custom_file_raw = (os.environ.get("SSL_CERT_FILE") or "").strip()
    if custom_file_raw:
        custom_file = Path(custom_file_raw)
        if custom_file.is_file() and custom_file != bundled:
            try:
                context.load_verify_locations(cafile=str(custom_file))
            except OSError:
                pass
    custom_dir_raw = (os.environ.get("SSL_CERT_DIR") or "").strip()
    if custom_dir_raw:
        custom_dir = Path(custom_dir_raw)
        if custom_dir.is_dir():
            try:
                context.load_verify_locations(capath=str(custom_dir))
            except OSError:
                pass
    return context


KASPI_SSL_CONTEXT = build_kaspi_ssl_context()


def httpx_runtime_options() -> dict[str, Any]:
    """Stable options shared by every local Kaspi HTTP client."""

    return {
        "verify": KASPI_SSL_CONTEXT,
        # Kaspi and CRM are contacted directly. Ignoring ambient proxy/CA
        # variables prevents another process from poisoning this Agent later.
        "trust_env": False,
    }
