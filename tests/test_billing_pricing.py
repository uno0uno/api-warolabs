"""Tests for SaaS MoR pricing matrix (#794 / #806 / #944)."""
from unittest.mock import patch

from app.core.billing_pricing import (
    ANNUAL_MULTIPLIER,
    DEFAULT_BILLING_SANDBOX_TENANT_SLUGS,
    EUROZONE_COUNTRIES,
    INTL_USD_30_ALLOWLIST,
    grandfather_active_annual,
    resolve_price_offer,
    resolve_price_segment,
    resolve_provider_environment,
    should_skip_mid_period_rebill,
)
from app.services.lemon_squeezy_service import configured_variant_id


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


def test_ls_variant_id_by_environment():
    offer = resolve_price_offer("CO")
    assert "MONTHLY_TEST" in offer.lemon_squeezy_variant_id("test")
    assert "MONTHLY_LIVE" in offer.lemon_squeezy_variant_id("prod")


def test_charge_basis_is_monthly_minors():
    assert resolve_price_offer("CO").monthly_amount_minor == 900
    assert resolve_price_offer("US").monthly_amount_minor == 3000
    assert resolve_price_offer("ES").monthly_amount_minor == 3000


def test_configured_variant_id_prefers_env_over_placeholder():
    offer = resolve_price_offer("CO")
    with patch("app.services.lemon_squeezy_service.settings") as mock_settings:
        mock_settings.lemon_squeezy_variant_usd_9_monthly_test = "var_monthly_test"
        assert configured_variant_id(offer, "test") == "var_monthly_test"


def test_configured_variant_id_falls_back_to_placeholder():
    offer = resolve_price_offer("CO")
    with patch("app.services.lemon_squeezy_service.settings") as mock_settings:
        mock_settings.lemon_squeezy_variant_usd_9_monthly_test = None
        assert configured_variant_id(offer, "test").startswith(
            "TODO_LEMON_SQUEEZY_VARIANT_USD_9_MONTHLY"
        )


def test_provider_environment_sandbox_env_forces_test_for_any_slug():
    """LEMON_SQUEEZY_ENVIRONMENT=sandbox → test for arbitrary tenants (#813)."""
    with patch("app.config.settings") as mock_settings:
        mock_settings.lemon_squeezy_environment = "sandbox"
        mock_settings.billing_sandbox_tenant_slugs = ""
        assert resolve_provider_environment(tenant_slug="bubablue") == "test"
        assert resolve_provider_environment(tenant_slug="any-tenant") == "test"


def test_provider_environment_production_allowlist_and_live():
    """Production mode: allowlist → test; others → prod (#813)."""
    assert DEFAULT_BILLING_SANDBOX_TENANT_SLUGS == frozenset({"waro-colombia", "warocolombia"})
    with patch("app.config.settings") as mock_settings:
        mock_settings.lemon_squeezy_environment = "production"
        mock_settings.billing_sandbox_tenant_slugs = ""
        assert resolve_provider_environment(tenant_slug="warocolombia") == "test"
        assert resolve_provider_environment(tenant_slug="waro-colombia") == "test"
        assert resolve_provider_environment(tenant_slug="waro") == "prod"
        assert resolve_provider_environment(tenant_slug="bubablue") == "prod"
        assert resolve_provider_environment(billing_test=True) == "test"

        mock_settings.billing_sandbox_tenant_slugs = "qa-slug,11111111-1111-1111-1111-111111111111"
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
