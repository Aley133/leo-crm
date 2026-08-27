from __future__ import annotations

import os
import threading
from pathlib import Path

from .session_client import OzonSessionHttpClient
from .session_profile import CurlProfile
from .session_store import SessionStore, utc_now


class OzonSessionUnavailableError(RuntimeError):
    """The local agent has no usable Ozon HTTP session yet."""


class OzonSessionResolver:
    """Resolve and validate the encrypted HTTP session used by the local agent.

    The session remains on the operator's computer.  CRM jobs and diagnostics
    never receive cookies or copied request headers.
    """

    def __init__(self, store: SessionStore | None = None) -> None:
        self.store = store or SessionStore()
        self._profile: CurlProfile | None = None
        self._lock = threading.Lock()

    def resolve(self, *, validate: bool = False) -> CurlProfile:
        with self._lock:
            if self._profile is not None and not validate:
                return self._profile

            candidates = self._candidates()
            errors: list[str] = []
            for source, loader in candidates:
                try:
                    profile = loader()
                    if validate:
                        self._validate(profile)
                    self._profile = profile
                    if source != "agent_store":
                        self.store.save(profile, {"imported_at": utc_now(), "source": source})
                    return profile
                except Exception as exc:
                    errors.append(f"{source}: {type(exc).__name__}")

            detail = ", ".join(errors[-3:]) if errors else "session not found"
            raise OzonSessionUnavailableError(
                "Ozon HTTP session is not configured. Import one Ozon search "
                "request (Copy as cURL) into the local agent. " + detail
            )

    def invalidate(self) -> None:
        with self._lock:
            self._profile = None

    def import_curl(self, curl_text: str, *, validate: bool = True) -> dict:
        profile = CurlProfile.parse(curl_text)
        if validate:
            self._validate(profile)
        meta = {"imported_at": utc_now(), "source": "curl_import"}
        saved = self.store.save(profile, meta)
        with self._lock:
            self._profile = profile
        return {**saved, "profile": profile.redacted_summary()}

    def _candidates(self):
        candidates = []
        if self.store.exists():
            candidates.append(("agent_store", lambda: self.store.load()[0]))

        curl_file = (os.getenv("OZON_SESSION_CURL_FILE") or "").strip()
        if curl_file:
            path = Path(curl_file).expanduser()
            candidates.append(("curl_file", lambda path=path: CurlProfile.parse(path.read_text(encoding="utf-8"))))

        session_file = (os.getenv("OZON_SESSION_FILE") or "").strip()
        if session_file:
            path = Path(session_file).expanduser()
            candidates.append(("session_file", lambda path=path: self._load_store_file(path)))

        # Compatibility with the proved lab: import its DPAPI session once,
        # then persist it in the agent-owned data directory.  Search is narrow
        # and bounded to conventional user folders.
        for path in self._legacy_paths():
            candidates.append((f"legacy:{path.parent.name}", lambda path=path: self._load_store_file(path)))
        return candidates

    @staticmethod
    def _load_store_file(path: Path) -> CurlProfile:
        if not path.is_file():
            raise FileNotFoundError(path)
        loaded = SessionStore(path.parent).load()
        if loaded is None:
            raise ValueError("empty session store")
        return loaded[0]

    @staticmethod
    def _legacy_paths() -> list[Path]:
        roots = [Path.cwd(), Path.home() / "Downloads", Path.home() / "Desktop"]
        found: list[Path] = []
        for root in roots:
            if not root.is_dir():
                continue
            patterns = (
                "kaspi-ozon-e2e-lab-v0_*/data/ozon_session.dpapi",
                "kaspi-ozon-e2e-lab-v0_*/kaspi-ozon-e2e-lab-v0_*/data/ozon_session.dpapi",
            )
            for pattern in patterns:
                found.extend(sorted(root.glob(pattern), reverse=True)[:4])
        return list(dict.fromkeys(found))[:8]

    @staticmethod
    def _validate(profile: CurlProfile) -> None:
        client = OzonSessionHttpClient(profile)
        try:
            result = client.search("Solgar", 1)
        finally:
            client.close()
        if not result.get("ok"):
            reason = result.get("reason") or "validation_failed"
            raise OzonSessionUnavailableError(f"Ozon HTTP session validation failed: {reason}")
