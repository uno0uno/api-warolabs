from app.core.country_locale import locale_from_country


def test_locale_from_country_core_markets():
    assert locale_from_country("CO") == "es"
    assert locale_from_country("US") == "en"
    assert locale_from_country("BR") == "pt"


def test_locale_from_country_latam_and_unknown():
    assert locale_from_country("MX") == "es"
    assert locale_from_country("AR") == "es"
    assert locale_from_country("PA") == "es"
    assert locale_from_country("DE") == "es"  # v1: other/unknown → es
    assert locale_from_country("ZZ") == "es"
    assert locale_from_country("") == "es"
    assert locale_from_country(None) == "es"
    assert locale_from_country(" us ") == "en"
