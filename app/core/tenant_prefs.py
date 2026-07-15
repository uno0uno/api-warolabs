"""Tenant localization and financial country/currency helpers.

Operational defaults preserve the Colombia product:
- locale: es
- currency_code: COP (display only; no FX)

Separate from fiscal/legal Colombia constraints (api-facturacion).
"""
from decimal import Decimal, InvalidOperation
from typing import Optional

DEFAULT_TENANT_LOCALE = "es"
DEFAULT_CURRENCY_CODE = "COP"
ALLOWED_LOCALES = frozenset({"es", "en"})
DEFAULT_TENANT_UI_LOCALE = "es"
ALLOWED_UI_LOCALES = frozenset({"es", "en", "pt", "fr", "de", "hi", "zh", "ar"})

# Financial country/currency catalog. Panama is the only supported country with
# more than one possible base currency. Keep this server-side so clients cannot
# invent combinations that change the meaning of historical amounts.
COUNTRY_CURRENCY_PAIRS = {
    "US": ("USD",),
    "CA": ("CAD",),
    "GB": ("GBP",),
    "AU": ("AUD",),
    "NZ": ("NZD",),
    "BR": ("BRL",),
    "DE": ("EUR",),
    "FR": ("EUR",),
    "NL": ("EUR",),
    "SG": ("SGD",),
    "AE": ("AED",),
    "IN": ("INR",),
    "CN": ("CNY",),
    "MX": ("MXN",),
    "ES": ("EUR",),
    "CO": ("COP",),
    "CR": ("CRC",),
    "UY": ("UYU",),
    "CL": ("CLP",),
    "PE": ("PEN",),
    "AR": ("ARS",),
    "DO": ("DOP",),
    "PA": ("USD", "PAB"),
}

# Public registration phone options. Keep this aligned with the countries that
# WARO can provision so clients do not maintain a separate dialing-code list.
COUNTRY_CALLING_CODES = {
    "US": 1,
    "CA": 1,
    "GB": 44,
    "AU": 61,
    "NZ": 64,
    "BR": 55,
    "DE": 49,
    "FR": 33,
    "NL": 31,
    "SG": 65,
    "AE": 971,
    "IN": 91,
    "CN": 86,
    "MX": 52,
    "ES": 34,
    "CO": 57,
    "CR": 506,
    "UY": 598,
    "CL": 56,
    "PE": 51,
    "AR": 54,
    "DO": 1,
    "PA": 507,
}
SUPPORTED_PHONE_COUNTRY_CODES = frozenset(COUNTRY_CALLING_CODES.values())
SUPPORTED_CURRENCY_MINOR_UNITS = {
    code: (0 if code == "CLP" else 2)
    for codes in COUNTRY_CURRENCY_PAIRS.values()
    for code in codes
}
SUPPORTED_COUNTRY_CODES = frozenset(COUNTRY_CURRENCY_PAIRS)
SUPPORTED_CURRENCY_CODES = frozenset(SUPPORTED_CURRENCY_MINOR_UNITS)


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
    """Return a supported normalized ISO 4217 currency code."""
    if value is None or not isinstance(value, str) or not value.strip():
        raise ValueError("currency_code must be a 3-letter ISO code")
    code = value.strip().upper()
    if len(code) != 3 or not code.isalpha() or code not in SUPPORTED_CURRENCY_CODES:
        supported = ", ".join(sorted(SUPPORTED_CURRENCY_CODES))
        raise ValueError(f"currency_code must be one of: {supported}")
    return code


def normalize_currency_code(value: Optional[str]) -> str:
    """Return a safe display currency code, falling back for legacy/invalid values."""
    try:
        return validate_currency_code(value)
    except ValueError:
        return DEFAULT_CURRENCY_CODE


def validate_country_code(value: Optional[str]) -> str:
    """Return a supported normalized ISO 3166-1 alpha-2 country code."""
    if value is None or not isinstance(value, str) or not value.strip():
        raise ValueError("country_code is required")
    code = value.strip().upper()
    if code not in SUPPORTED_COUNTRY_CODES:
        supported = ", ".join(sorted(SUPPORTED_COUNTRY_CODES))
        raise ValueError(f"country_code must be one of: {supported}")
    return code


def validate_country_currency_pair(country: Optional[str], currency: Optional[str]) -> tuple[str, str]:
    """Validate and normalize an allowed financial country/base-currency pair."""
    country_code = validate_country_code(country)
    currency_code = validate_currency_code(currency)
    if currency_code not in COUNTRY_CURRENCY_PAIRS[country_code]:
        allowed = ", ".join(COUNTRY_CURRENCY_PAIRS[country_code])
        raise ValueError(f"base_currency_code for {country_code} must be one of: {allowed}")
    return country_code, currency_code


def currency_minor_units(currency: Optional[str]) -> int:
    """Return ISO minor units for a supported currency, defaulting safely to COP."""
    code = normalize_currency_code(currency)
    return SUPPORTED_CURRENCY_MINOR_UNITS[code]


def validate_currency_amount(value, currency: Optional[str]) -> Decimal:
    """Reject amounts with more fractional digits than the base currency allows."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("amount must be a decimal value") from exc
    units = currency_minor_units(currency)
    quantum = Decimal(1).scaleb(-units)
    if amount != amount.quantize(quantum):
        raise ValueError(f"{normalize_currency_code(currency)} supports {units} decimal places")
    return amount
