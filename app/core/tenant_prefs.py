"""Tenant localization prefs helpers (locale + display currency).

Operational defaults preserve the Colombia product:
- locale: es
- currency_code: COP (display only; no FX)

Separate from fiscal/legal Colombia constraints (api-facturacion).
"""
from typing import Optional

DEFAULT_TENANT_LOCALE = "es"
DEFAULT_CURRENCY_CODE = "COP"
ALLOWED_LOCALES = frozenset({"es", "en"})
DEFAULT_TENANT_UI_LOCALE = "es"
ALLOWED_UI_LOCALES = frozenset({"es", "en", "pt", "fr", "de", "hi", "zh", "ar"})


def validate_locale(value: Optional[str]) -> str:
    """Return a normalized locale or raise ValueError for user input."""
    if value is None or not isinstance(value, str) or not value.strip():
        raise ValueError("locale must be one of: es, en")
    locale = value.strip().lower()
    if locale not in ALLOWED_LOCALES:
        raise ValueError("locale must be one of: es, en")
    return locale


def normalize_locale(value: Optional[str]) -> str:
    """Return a safe tenant locale, falling back for legacy/invalid values."""
    try:
        return validate_locale(value)
    except ValueError:
        return DEFAULT_TENANT_LOCALE


def validate_ui_locale(value: Optional[str]) -> str:
    """Validate the tenant-wide frontend locale without changing receipt locale."""
    if value is None or not isinstance(value, str) or not value.strip():
        raise ValueError(
            "ui_locale must be one of: es, en, pt, fr, de, hi, zh, ar"
        )
    locale = value.strip().lower()
    if locale not in ALLOWED_UI_LOCALES:
        raise ValueError(
            "ui_locale must be one of: es, en, pt, fr, de, hi, zh, ar"
        )
    return locale


def normalize_ui_locale(value: Optional[str]) -> str:
    """Return a safe frontend locale, falling back to Spanish."""
    try:
        return validate_ui_locale(value)
    except ValueError:
        return DEFAULT_TENANT_UI_LOCALE


def validate_currency_code(value: Optional[str]) -> str:
    """Return a normalized ISO 4217-ish currency code or raise ValueError."""
    if value is None or not isinstance(value, str) or not value.strip():
        raise ValueError("currency_code must be a 3-letter ISO code")
    code = value.strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise ValueError("currency_code must be a 3-letter ISO code")
    return code


def normalize_currency_code(value: Optional[str]) -> str:
    """Return a safe display currency code, falling back for legacy/invalid values."""
    try:
        return validate_currency_code(value)
    except ValueError:
        return DEFAULT_CURRENCY_CODE
