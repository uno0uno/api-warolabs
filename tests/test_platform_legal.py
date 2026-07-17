"""Platform legal print payload is env-driven (WARO + Matias/LOPEZSOFT)."""
import os

import pytest


@pytest.fixture
def platform_env(monkeypatch):
    monkeypatch.setenv("WARO_LEGAL_COMMERCIAL_NAME", "WARO COLOMBIA")
    monkeypatch.setenv("WARO_LEGAL_LEGAL_NAME", "AREVALO RAMIREZ ANDERSON EDUARDO")
    monkeypatch.setenv("WARO_LEGAL_NIT", "700128766-3")
    monkeypatch.setenv("WARO_LEGAL_WEBSITE", "warocol.com")
    monkeypatch.setenv("WARO_LEGAL_IVA_LABEL", "No responsable de IVA")
    monkeypatch.setenv("FACTURADOR_LEGAL_BRAND_NAME", "Matias API")
    monkeypatch.setenv("FACTURADOR_LEGAL_LEGAL_NAME", "LOPEZSOFT S.A.S.")
    monkeypatch.setenv("FACTURADOR_LEGAL_NIT", "901.091.403-2")
    # Reload settings module fields — Settings is constructed at import.
    # platform_legal reads settings at call time; force re-instantiate.
    from app import config as config_mod
    config_mod.settings = config_mod.Settings()
    yield
    config_mod.settings = config_mod.Settings()


def test_print_payload_has_waro_and_matias_without_pii_phones(platform_env):
    from app.core.platform_legal import get_platform_legal_for_print

    payload = get_platform_legal_for_print()
    assert payload["software"]["commercial_name"] == "WARO COLOMBIA"
    assert payload["software"]["nit"] == "700128766-3"
    assert payload["software"]["website"] == "warocol.com"
    assert payload["software"]["role_label"] == "Software de gestión"
    assert payload["software"]["not_issuer_disclaimer"]
    # print payload must not expose personal email/phone keys
    assert "email" not in payload["software"]
    assert "phones" not in payload["software"]
    assert "document_number" not in payload["software"]
    assert "legal_name" not in payload["software"]

    assert payload["facturador"]["brand_name"] == "Matias API"
    assert payload["facturador"]["legal_name"] == "LOPEZSOFT S.A.S."
    assert payload["facturador"]["nit"] == "901.091.403-2"
    assert payload["facturador"]["slug"] == "matias"


def test_footer_with_fe_mentions_facturador(platform_env):
    from app.core.platform_legal import waro_platform_footer_text

    text = waro_platform_footer_text(with_fe_note=True)
    assert "WARO COLOMBIA" in text
    assert "700128766-3" in text
    assert "warocol.com" in text
    assert "AREVALO RAMIREZ" not in text
    assert "Matias API" in text
    assert "901.091.403-2" in text
    assert "LOPEZSOFT" in text
    assert "No es el emisor" in text


def test_footer_without_fe_no_matias_required(platform_env):
    from app.core.platform_legal import waro_platform_footer_text

    text = waro_platform_footer_text(with_fe_note=False)
    assert "WARO COLOMBIA" in text
    assert "warocol.com" in text
    assert "AREVALO RAMIREZ" not in text
    assert "Comprobante del establecimiento" in text
