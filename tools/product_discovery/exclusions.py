"""Canonical brand keys: exact names, independent of case and spacing."""
import unicodedata


def normalize_brand(value: str | None) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()
