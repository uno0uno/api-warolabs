"""Tenant locale + currency prefs (warocol.com#1599 / epic #1598 B1)."""
import pytest
from pydantic import ValidationError

from app.core.tenant_prefs import (
    DEFAULT_CURRENCY_CODE,
    DEFAULT_TENANT_LOCALE,
    normalize_currency_code,
    normalize_locale,
    validate_currency_code,
    validate_locale,
)
from app.models.tenant_public_profile import (
    TenantPublicProfileBase,
    TenantPublicProfileUpdate,
)


def test_validate_locale_accepts_es_en_and_normalizes_case():
    assert validate_locale("es") == "es"
    assert validate_locale("EN") == "en"
    assert validate_locale(" Es ") == "es"


def test_validate_locale_rejects_unknown():
    with pytest.raises(ValueError, match="locale must be one of"):
        validate_locale("fr")
    with pytest.raises(ValueError, match="locale must be one of"):
        validate_locale("")
    with pytest.raises(ValueError, match="locale must be one of"):
        validate_locale(None)


def test_normalize_locale_defaults_for_missing_or_junk():
    assert normalize_locale(None) == DEFAULT_TENANT_LOCALE
    assert normalize_locale("") == DEFAULT_TENANT_LOCALE
    assert normalize_locale("xx") == DEFAULT_TENANT_LOCALE
    assert normalize_locale("en") == "en"


def test_validate_currency_code_accepts_iso_and_uppercases():
    assert validate_currency_code("COP") == "COP"
    assert validate_currency_code("usd") == "USD"
    assert validate_currency_code(" MxN ") == "MXN"
    assert validate_currency_code("clp") == "CLP"


def test_validate_currency_code_rejects_invalid():
    with pytest.raises(ValueError):
        validate_currency_code("CO")
    with pytest.raises(ValueError):
        validate_currency_code("123")
    with pytest.raises(ValueError, match="3-letter"):
        validate_currency_code(None)
    with pytest.raises(ValueError, match="must be one of"):
        validate_currency_code("ZZZ")


def test_normalize_currency_code_defaults_for_missing_or_junk():
    assert normalize_currency_code(None) == DEFAULT_CURRENCY_CODE
    assert normalize_currency_code("") == DEFAULT_CURRENCY_CODE
    assert normalize_currency_code("XX") == DEFAULT_CURRENCY_CODE  # not alpha length ok? XX is alpha len2
    assert normalize_currency_code("ZZZ") == DEFAULT_CURRENCY_CODE
    assert normalize_currency_code("cop") == "COP"


def test_profile_base_defaults_are_colombia_product():
    # Minimal required fields for base model
    profile = TenantPublicProfileBase(slug="demo", display_name="Demo")
    assert profile.locale == "es"
    assert profile.currency_code == "COP"
    assert profile.timezone == "America/Bogota"


def test_profile_base_rejects_invalid_locale():
    with pytest.raises(ValidationError):
        TenantPublicProfileBase(slug="demo", display_name="Demo", locale="fr")


def test_profile_update_round_trip_fields():
    update = TenantPublicProfileUpdate(locale="en")
    dumped = update.model_dump(exclude_unset=True)
    assert dumped["locale"] == "en"


def test_profile_update_rejects_financial_fields():
    with pytest.raises(ValidationError):
        TenantPublicProfileUpdate(currency_code="peso")
    with pytest.raises(ValidationError, match="financial-profile"):
        TenantPublicProfileUpdate(currency_code="USD")
    with pytest.raises(ValidationError, match="financial-profile"):
        TenantPublicProfileUpdate(country="United States")
