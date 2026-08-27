from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ROOT
from .session_profile import CurlProfile

STORE_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi_encrypt(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data)
    in_blob = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    out_blob = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "LEO CRM Ozon HTTP session",
        None,
        None,
        None,
        0x01,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_decrypt(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data)
    in_blob = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    out_blob = _DataBlob()
    description = ctypes.c_wchar_p()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        ctypes.byref(description),
        None,
        None,
        None,
        0x01,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if description:
            kernel32.LocalFree(description)
        kernel32.LocalFree(out_blob.pbData)


class SessionStore:
    """Persist an Ozon HTTP session locally.

    On Windows the payload is encrypted with DPAPI and can only be decrypted by
    the same Windows user profile. Other platforms use a chmod-0600 local JSON
    fallback for development/tests.
    """

    def __init__(self, data_dir: Path | None = None):
        self.data_dir = Path(data_dir or (ROOT / "data"))
        self.dpapi_path = self.data_dir / "ozon_session.dpapi"
        self.plain_path = self.data_dir / "ozon_session.local.json"

    @property
    def path(self) -> Path:
        return self.dpapi_path if os.name == "nt" else self.plain_path

    @property
    def protection(self) -> str:
        return "windows_dpapi" if os.name == "nt" else "local_file_0600"

    def exists(self) -> bool:
        return self.path.is_file()

    def save(self, profile: CurlProfile, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STORE_VERSION,
            "profile": profile.to_dict(),
            "meta": dict(meta or {}),
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if os.name == "nt":
            protected = _dpapi_encrypt(raw)
            envelope = {
                "format": "windows-dpapi-b64-v1",
                "blob": base64.b64encode(protected).decode("ascii"),
            }
            self.dpapi_path.write_text(json.dumps(envelope, separators=(",", ":")), encoding="utf-8")
        else:
            self.plain_path.write_bytes(raw)
            try:
                os.chmod(self.plain_path, 0o600)
            except Exception:
                pass
        return self.describe(meta)

    def load(self) -> tuple[CurlProfile, dict[str, Any]] | None:
        path = self.path
        if not path.is_file():
            return None
        if os.name == "nt":
            envelope = json.loads(path.read_text(encoding="utf-8"))
            if envelope.get("format") != "windows-dpapi-b64-v1":
                raise ValueError("Неизвестный формат сохранённой Ozon session")
            raw = _dpapi_decrypt(base64.b64decode(envelope["blob"]))
        else:
            raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        if int(payload.get("version") or 0) != STORE_VERSION:
            raise ValueError("Неподдерживаемая версия сохранённой Ozon session")
        profile = CurlProfile.from_dict(payload.get("profile") or {})
        meta = dict(payload.get("meta") or {})
        return profile, meta

    def clear(self) -> None:
        for path in (self.dpapi_path, self.plain_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def describe(self, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "exists": self.exists(),
            "protection": self.protection,
            "filename": self.path.name,
            "imported_at": (meta or {}).get("imported_at"),
            "last_check_at": (meta or {}).get("last_check_at"),
            "last_success_at": (meta or {}).get("last_success_at"),
            "last_http_status": (meta or {}).get("last_http_status"),
            "last_blocked": (meta or {}).get("last_blocked"),
            "successful_requests": int((meta or {}).get("successful_requests") or 0),
        }
