from pathlib import Path

from app.core.country_locale import (
    default_currency_for_country,
    locale_from_country,
    normalize_article_country_code,
)


def test_normalize_article_country_code_iso_and_names():
    assert normalize_article_country_code("ES") == "ES"
    assert normalize_article_country_code("Spain") == "ES"
    assert normalize_article_country_code("España") == "ES"
    assert normalize_article_country_code("US") == "US"
    assert normalize_article_country_code("USA") == "US"
    assert normalize_article_country_code("United States") == "US"
    assert normalize_article_country_code("Colombia") == "CO"
    assert normalize_article_country_code("CO") == "CO"
    assert normalize_article_country_code("Mexico") == "MX"


def test_latam_region_has_no_iso_code():
    assert normalize_article_country_code("LATAM") is None
    assert normalize_article_country_code("latam") is None
    assert normalize_article_country_code("") is None
    assert normalize_article_country_code(None) is None


def test_latam_fallback_locale_and_currency():
    assert locale_from_country(None) == "es"
    assert default_currency_for_country(None) == "COP"
    assert default_currency_for_country("ES") == "EUR"
    assert default_currency_for_country("US") == "USD"
    assert default_currency_for_country("CO") == "COP"


def test_migration_adds_nullable_country_code_without_dropping_country():
    sql = Path("migrations/125_articles_country_code.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS country_code CHAR(2)" in sql
    assert "DROP COLUMN" not in sql.upper()
    assert "ALTER TABLE articles" in sql
