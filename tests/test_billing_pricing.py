"""Tests for Paddle pricing matrix (#794 / #806)."""
from unittest.mock import patch

from app.core.billing_pricing import (
    ANNUAL_MULTIPLIER,
    EUROZONE_COUNTRIES,
    INTL_USD_30_ALLOWLIST,
    PADDLE_SANDBOX_TENANT_SLUGS,
    grandfather_active_annual,
    resolve_price_offer,
    resolve_price_segment,
    resolve_provider_environment,
    should_skip_mid_period_rebill,
)
from app.services.paddle_service import configured_price_id


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
    assert "MONTHLY_TEST" in offer.paddle_price_id("test")
    assert "MONTHLY_LIVE" in offer.paddle_price_id("prod")


def test_charge_basis_is_monthly_minors():
    assert resolve_price_offer("CO").monthly_amount_minor == 900
    assert resolve_price_offer("US").monthly_amount_minor == 3000
    assert resolve_price_offer("ES").monthly_amount_minor == 3000


def test_configured_price_id_prefers_monthly_over_annual():
    offer = resolve_price_offer("CO")
    with patch("app.services.paddle_service.settings") as mock_settings:
        mock_settings.paddle_price_usd_9_monthly_test = "pri_monthly_test"
        mock_settings.paddle_price_usd_9_annual_test = "pri_annual_test"
        assert configured_price_id(offer, "test") == "pri_monthly_test"


def test_configured_price_id_falls_back_to_annual_then_placeholder():
    offer = resolve_price_offer("CO")
    with patch("app.services.paddle_service.settings") as mock_settings:
        mock_settings.paddle_price_usd_9_monthly_test = None
        mock_settings.paddle_price_usd_9_annual_test = "pri_annual_only"
        assert configured_price_id(offer, "test") == "pri_annual_only"
        mock_settings.paddle_price_usd_9_annual_test = None
        assert configured_price_id(offer, "test").startswith("TODO_PADDLE_PRICE_USD_9_MONTHLY")


def test_provider_environment_sandbox_env_forces_test_for_any_slug():
    """PADDLE_ENVIRONMENT=sandbox → test for arbitrary tenants (#813)."""
    with patch("app.config.settings") as mock_settings:
        mock_settings.paddle_environment = "sandbox"
        mock_settings.paddle_sandbox_tenant_slugs = ""
        assert resolve_provider_environment(tenant_slug="bubablue") == "test"
        assert resolve_provider_environment(tenant_slug="any-tenant") == "test"


def test_provider_environment_production_allowlist_and_live():
    """Production mode: allowlist → test; others → prod (#813)."""
    assert PADDLE_SANDBOX_TENANT_SLUGS == frozenset({"waro-colombia", "warocolombia"})
    with patch("app.config.settings") as mock_settings:
        mock_settings.paddle_environment = "production"
        mock_settings.paddle_sandbox_tenant_slugs = ""
        assert resolve_provider_environment(tenant_slug="warocolombia") == "test"
        assert resolve_provider_environment(tenant_slug="waro-colombia") == "test"
        assert resolve_provider_environment(tenant_slug="waro") == "prod"
        assert resolve_provider_environment(tenant_slug="bubablue") == "prod"
        assert resolve_provider_environment(billing_test=True) == "test"

        mock_settings.paddle_sandbox_tenant_slugs = "qa-slug,11111111-1111-1111-1111-111111111111"
        assert resolve_provider_environment(tenant_slug="qa-slug") == "test"
        assert (
            resolve_provider_environment(tenant_id="11111111-1111-1111-1111-111111111111")
            == "test"
        )
        assert resolve_provider_environment(tenant_slug="bubablue") == "prod"


def test_blank_country_defaults_to_co_usd_9():
    assert resolve_price_segment(None) == "usd_9"
    assert resolve_price_segment("") == "usd_9"
    assert resolve_price_offer(None).annual_amount_minor == 9000


def test_grandfather_active_annual():
    assert should_skip_mid_period_rebill(current_period_end_in_future=True) is True
    assert should_skip_mid_period_rebill(current_period_end_in_future=False) is False
    assert grandfather_active_annual(current_period_end_in_future=True) is True
    assert grandfather_active_annual(current_period_end_in_future=False) is False
