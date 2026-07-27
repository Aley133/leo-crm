from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


class KaspiCredentialConfigurationError(RuntimeError):
    pass


class KaspiCredentialDecryptError(RuntimeError):
    pass


def _fernet() -> Fernet:
    raw_key = os.getenv("KASPI_CREDENTIALS_KEY", "").strip()
    if not raw_key:
        raise KaspiCredentialConfigurationError(
            "KASPI_CREDENTIALS_KEY is not configured"
        )
    try:
        return Fernet(raw_key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise KaspiCredentialConfigurationError(
            "KASPI_CREDENTIALS_KEY must be a valid Fernet key"
        ) from exc


def encrypt_api_token(raw_token: str) -> str:
    token = raw_token.strip()
    if not token:
        raise ValueError("Kaspi API token is required")
    return _fernet().encrypt(token.encode("utf-8")).decode("ascii")


def decrypt_api_token(encrypted_token: str) -> str:
    try:
        return _fernet().decrypt(encrypted_token.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise KaspiCredentialDecryptError(
            "Stored Kaspi API token cannot be decrypted"
        ) from exc
