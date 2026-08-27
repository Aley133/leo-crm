from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _runtime_root() -> Path:
    configured = (os.getenv("OZON_HTTP_DATA_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        return Path(os.getenv("APPDATA") or Path.home()) / "LEO CRM" / "ozon-http"
    return Path(os.getenv("XDG_STATE_HOME") or (Path.home() / ".local" / "state")) / "leo-crm" / "ozon-http"


# Runtime diagnostics and the encrypted session live outside the downloaded
# source tree, so agent upgrades never overwrite them.
ROOT = _runtime_root()


@dataclass(slots=True)
class Config:
    host: str = "https://www.ozon.ru"
    timeout: float = 25.0
    impersonate: str = "chrome"
    bootstrap: bool = True
    max_results: int = 48
    expected_currency: str = "KZT"

    @classmethod
    def load(cls) -> "Config":
        return cls(
            host=(os.getenv("OZON_HOST") or "https://www.ozon.ru").rstrip("/"),
            timeout=float(os.getenv("OZON_TIMEOUT") or 25),
            impersonate=os.getenv("OZON_IMPERSONATE") or "chrome",
            bootstrap=(os.getenv("OZON_BOOTSTRAP") or "1").strip().lower() not in {"0", "false", "no"},
            max_results=max(1, min(120, int(os.getenv("OZON_MAX_RESULTS") or 48))),
            expected_currency=(os.getenv("OZON_EXPECTED_CURRENCY") or "KZT").strip().upper(),
        )
