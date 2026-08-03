"""Map business country codes to default UI locales (onboarding v1).

Persisted preferred_locale / tenant ui_locale follow the final country, not cookie.
Align with front_nuxt/utils/countryLocale.ts.
"""
from __future__ import annotations

from typing import Optional

from app.core.tenant_prefs import DEFAULT_TENANT_UI_LOCALE, validate_ui_locale

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
