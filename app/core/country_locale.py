"""Map business country codes to default UI locales (onboarding v1).

Persisted preferred_locale / tenant ui_locale follow the final country, not cookie.
Align with front_nuxt/utils/countryLocale.ts.
"""
from __future__ import annotations

from typing import Optional

from app.core.tenant_prefs import (
    COUNTRY_CURRENCY_PAIRS,
    DEFAULT_CURRENCY_CODE,
    DEFAULT_TENANT_UI_LOCALE,
    validate_currency_code,
    validate_ui_locale,
)

# Spanish-speaking markets in the financial catalog (epic #2100 v1).
ES_LATAM_COUNTRY_CODES = frozenset({
    "CO",
    "MX",
    "CR",
    "UY",
    "CL",
    "PE",
    "AR",
    "DO",
    "PA",
    "ES",
})

# Display labels for tenant_public_profiles.country (legacy default was "Colombia").
COUNTRY_DISPLAY_NAMES = {
    "US": "United States",
    "CA": "Canada",
    "GB": "United Kingdom",
    "AU": "Australia",
    "NZ": "New Zealand",
    "BR": "Brazil",
    "DE": "Germany",
    "FR": "France",
    "NL": "Netherlands",
    "SG": "Singapore",
    "AE": "United Arab Emirates",
    "IN": "India",
    "CN": "China",
    "MX": "Mexico",
    "ES": "Spain",
    "CO": "Colombia",
    "CR": "Costa Rica",
    "UY": "Uruguay",
    "CL": "Chile",
    "PE": "Peru",
    "AR": "Argentina",
    "DO": "Dominican Republic",
    "PA": "Panama",
}


def locale_from_country(country_code: Optional[str]) -> str:
    """Return UI locale for a business country. Unknown / empty → es."""
    code = str(country_code or "").strip().upper()
    if code == "US":
        return validate_ui_locale("en")
    if code == "BR":
        return validate_ui_locale("pt")
    if code in ES_LATAM_COUNTRY_CODES:
        return validate_ui_locale("es")
    return validate_ui_locale(DEFAULT_TENANT_UI_LOCALE)


_LATAM_REGION_LABELS = frozenset({
    "latam",
    "latin america",
    "latinoamerica",
    "latinoamérica",
    "américa latina",
    "america latina",
})

_COUNTRY_NAME_ALIASES = {
    "usa": "US",
    "u.s.": "US",
    "u.s.a.": "US",
    "united states of america": "US",
    "uk": "GB",
    "england": "GB",
    "españa": "ES",
    "espana": "ES",
    "méxico": "MX",
    "mexico": "MX",
    "brasil": "BR",
    "col": "CO",
}

_DISPLAY_NAME_TO_CODE = {
    name.casefold(): code for code, name in COUNTRY_DISPLAY_NAMES.items()
}


def normalize_article_country_code(value: Optional[str]) -> Optional[str]:
    """Map free-text articles.country to ISO-2, or None for region/unknown (LATAM)."""
    raw = str(value or "").strip()
    if not raw:
        return None
    folded = raw.casefold()
    if folded in _LATAM_REGION_LABELS:
        return None
    if len(raw) == 2:
        code = raw.upper()
        if code in COUNTRY_CURRENCY_PAIRS:
            return code
    alias = _COUNTRY_NAME_ALIASES.get(folded)
    if alias:
        return alias
    mapped = _DISPLAY_NAME_TO_CODE.get(folded)
    if mapped:
        return mapped
    return None


def public_country_name(country_code: Optional[str]) -> str:
    """Human-readable country for tenant_public_profiles.country."""
    code = str(country_code or "").strip().upper()
    if code in COUNTRY_DISPLAY_NAMES:
        return COUNTRY_DISPLAY_NAMES[code]
    return COUNTRY_DISPLAY_NAMES["CO"]


def default_currency_for_country(country_code: Optional[str]) -> str:
    """Primary catalog currency for a country (first allowed pair)."""
    code = str(country_code or "").strip().upper()
    currencies = COUNTRY_CURRENCY_PAIRS.get(code)
    if not currencies:
        return DEFAULT_CURRENCY_CODE
    return currencies[0]


def resolve_public_currency(
    country_code: Optional[str],
    currency_code: Optional[str] = None,
) -> str:
    """Prefer validated currency; otherwise catalog default for the country."""
    if currency_code:
        try:
            return validate_currency_code(currency_code)
        except ValueError:
            pass
    return default_currency_for_country(country_code)
