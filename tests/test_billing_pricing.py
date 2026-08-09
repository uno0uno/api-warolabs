"""Tests for Paddle pricing matrix (#794)."""
from app.core.billing_pricing import (
    ANNUAL_MULTIPLIER,
    EUROZONE_COUNTRIES,
    INTL_USD_30_ALLOWLIST,
    grandfather_active_annual,
    resolve_price_offer,
    resolve_price_segment,
    resolve_provider_environment,
)


def test_eurozone_maps_to_eur_30():
    for code in ("ES", "DE", "FR", "NL"):
        assert code in EUROZONE_COUNTRIES
        assert resolve_price_segment(code) == "eur_30"
        offer = resolve_price_offer(code)
        assert offer.currency == "EUR"
        assert offer.monthly_amount_minor == 3000
        assert offer.annual_amount_minor == 3000 * ANNUAL_MULTIPLIER


def test_dollarized_maps_to_usd_30():
    for code in ("US", "PA"):
        assert resolve_price_segment(code) == "usd_30"
        offer = resolve_price_offer(code)
        assert offer.currency == "USD"
        assert offer.monthly_amount_minor == 3000
        assert offer.annual_amount_minor == 30000


def test_latam_maps_to_usd_9():
    for code in ("CO", "MX", "PE", "CL", "AR", "BR", "CR", "UY", "DO"):
        assert resolve_price_segment(code) == "usd_9"
        offer = resolve_price_offer(code)
        assert offer.currency == "USD"
        assert offer.monthly_amount_minor == 900
        assert offer.annual_amount_minor == 9000


def test_intl_allowlist_maps_to_usd_30():
    for code in INTL_USD_30_ALLOWLIST:
        assert resolve_price_segment(code) == "usd_30"


def test_in_cn_stay_usd_9():
    assert resolve_price_segment("IN") == "usd_9"
    assert resolve_price_segment("CN") == "usd_9"


def test_paddle_price_id_by_environment():
    offer = resolve_price_offer("CO")
    assert "TEST" in offer.paddle_price_id("test")
    assert "LIVE" in offer.paddle_price_id("prod")


def test_provider_environment_sandbox_slugs_and_flag():
    assert resolve_provider_environment(tenant_slug="warocolombia") == "test"
    assert resolve_provider_environment(tenant_slug="waro-colombia") == "test"
    assert resolve_provider_environment(billing_test=True) == "test"
    assert resolve_provider_environment(tenant_slug="bubablue") == "prod"
    assert resolve_provider_environment(tenant_slug="bubablue", billing_test=False) == "prod"


def test_grandfather_active_annual():
    assert grandfather_active_annual(current_period_end_in_future=True) is True
    assert grandfather_active_annual(current_period_end_in_future=False) is False
