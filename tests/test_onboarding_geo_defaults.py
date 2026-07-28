"""Geo defaults at onboarding — warocol.com#1854."""
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.timezones import (
    DEFAULT_TENANT_TIMEZONE,
    default_timezone_for_country,
    seed_tenant_timezone_from_country,
)
from app.models.auth import RegistrationMagicLinkRequest
from app.models.onboarding import OnboardingBusinessProfileUpdate
from app.routers.auth import registration_options
from app.services.onboarding_service import update_onboarding_financial_profile


def test_default_timezone_for_country_map():
    assert default_timezone_for_country("CO") == "America/Bogota"
    assert default_timezone_for_country("PA") == "America/Panama"
    assert default_timezone_for_country("US") == "America/New_York"
    assert default_timezone_for_country("CA") == "America/Toronto"
    assert default_timezone_for_country("xx") == DEFAULT_TENANT_TIMEZONE


def test_registration_requires_jurisdiction_for_us():
    with pytest.raises(ValidationError):
        RegistrationMagicLinkRequest(
            email="us@example.com",
            phone_country_code=1,
            phone_number="4155551212",
            consent=True,
            business_name="Cafe Mission",
            country_code="US",
            base_currency_code="USD",
        )
    ok = RegistrationMagicLinkRequest(
        email="us@example.com",
        phone_country_code=1,
        phone_number="4155551212",
        consent=True,
        business_name="Cafe Mission",
        country_code="US",
        base_currency_code="USD",
        tax_jurisdiction_code="tx",
    )
    assert ok.tax_jurisdiction_code == "TX"


def test_registration_clears_jurisdiction_for_wave1():
    ok = RegistrationMagicLinkRequest(
        email="pa@example.com",
        phone_country_code=57,
        phone_number="3001234567",
        consent=True,
        business_name="Cafe Panama",
        country_code="PA",
        base_currency_code="USD",
        tax_jurisdiction_code="TX",
    )
    assert ok.tax_jurisdiction_code is None


@pytest.mark.asyncio
async def test_registration_options_include_us_ca_jurisdictions():
    result = await registration_options()
    assert "US" in result["tax_jurisdictions"]
    assert "CA" in result["tax_jurisdictions"]
    assert len(result["tax_jurisdictions"]["US"]) == 51
    assert len(result["tax_jurisdictions"]["CA"]) == 13


class _TzConn:
    def __init__(self, *, update_count=0, tenant=None):
        self.update_count = update_count
        self.tenant = tenant or {"slug": "cafe-pa", "name": "Cafe PA"}
        self.queries = []

    async def execute(self, query, *args):
        self.queries.append(("execute", query, args))
        if query.strip().upper().startswith("UPDATE"):
            return f"UPDATE {self.update_count}"
        return "INSERT 0 1"

    async def fetchrow(self, query, *args):
        self.queries.append(("fetchrow", query, args))
        return self.tenant


@pytest.mark.asyncio
async def test_seed_timezone_only_replaces_default():
    conn = _TzConn(update_count=0)
    tz = await seed_tenant_timezone_from_country(conn, uuid4(), "PA")
    assert tz == "America/Panama"
    assert any("INSERT INTO tenant_public_profiles" in q for kind, q, _ in conn.queries if kind == "execute")
    insert_args = next(args for kind, q, args in conn.queries if kind == "execute" and "INSERT" in q)
    assert insert_args[1] == "cafe-pa"
    assert insert_args[3] == "America/Panama"

    conn2 = _TzConn(update_count=1)
    tz2 = await seed_tenant_timezone_from_country(conn2, uuid4(), "US")
    assert tz2 == "America/New_York"
    assert any(kind == "execute" and "UPDATE tenant_public_profiles" in q for kind, q, _ in conn2.queries)
    assert not any(kind == "execute" and "INSERT" in q for kind, q, _ in conn2.queries)


class _OnboardingConn:
    def __init__(self):
        self.queries = []
        self.tax = {"tax_lines": None, "tax_jurisdiction_code": None}

    async def fetchrow(self, query, *args):
        self.queries.append(("fetchrow", query, args))
        if "FROM tenants t" in query and "FOR UPDATE" in query:
            return {
                "lifecycle_status": "pending",
                "business_name": "Cafe",
                "state": "business_profile_pending",
            }
        if "SELECT slug, name FROM tenants" in query:
            return {"slug": "cafe-test", "name": "Cafe"}
        if "INSERT INTO tenant_financial_profiles" in query:
            return {
                "profile_tenant_id": args[0],
                "country_code": args[1],
                "base_currency_code": args[2],
                "accounting_localization": args[3],
                "document_mode": args[4],
                "fiscal_provider": args[5],
                "selection_revision": 1,
                "profile_created_at": None,
                "profile_updated_at": None,
            }
        if "SELECT tax_lines FROM tenant_tax_config" in query:
            return dict(self.tax)
        if "UPDATE tenant_tax_config" in query and "tax_jurisdiction_code" in query:
            self.tax = {
                "tax_lines": args[2],
                "tax_jurisdiction_code": args[1],
                "category_map": args[3],
            }
            return {"tenant_id": args[0], **self.tax}
        return None

    async def execute(self, query, *args):
        self.queries.append(("execute", query, args))
        if query.strip().upper().startswith("UPDATE tenant_public_profiles"):
            return "UPDATE 0"
        if "UPDATE tenant_tax_config" in query and "tax_lines" in query:
            self.tax["tax_lines"] = args[1]
            return "UPDATE 1"
        return "OK"


@pytest.mark.asyncio
async def test_onboarding_us_requires_and_seeds_jurisdiction(monkeypatch):
    async def _promote(_conn, _tenant_id):
        return "terms_pending"

    async def _seed_accounts(_c, _tenant_id):
        return None

    monkeypatch.setattr(
        "app.services.onboarding_service._promote_onboarding_identity",
        _promote,
    )
    monkeypatch.setattr(
        "app.services.onboarding_service.financial_service.seed_tenant_accounts",
        _seed_accounts,
    )

    with pytest.raises(HTTPException) as missing:
        await update_onboarding_financial_profile(
            _OnboardingConn(),
            uuid4(),
            OnboardingBusinessProfileUpdate(
                businessName="Cafe US",
                country_code="US",
                base_currency_code="USD",
            ),
        )
    assert missing.value.status_code == 422

    conn = _OnboardingConn()
    result = await update_onboarding_financial_profile(
        conn,
        uuid4(),
        OnboardingBusinessProfileUpdate(
            businessName="Cafe US",
            country_code="US",
            base_currency_code="USD",
            tax_jurisdiction_code="TX",
        ),
    )
    assert result.data.profile.country_code == "US"
    assert conn.tax["tax_jurisdiction_code"] == "TX"
    assert any(
        "tenant_public_profiles" in q for kind, q, _ in conn.queries if kind == "execute"
    )


@pytest.mark.asyncio
async def test_onboarding_pa_seeds_wave1_and_timezone(monkeypatch):
    async def _promote(_conn, _tenant_id):
        return "terms_pending"

    async def _seed_accounts(_c, _tenant_id):
        return None

    monkeypatch.setattr(
        "app.services.onboarding_service._promote_onboarding_identity",
        _promote,
    )
    monkeypatch.setattr(
        "app.services.onboarding_service.financial_service.seed_tenant_accounts",
        _seed_accounts,
    )

    conn = _OnboardingConn()
    result = await update_onboarding_financial_profile(
        conn,
        uuid4(),
        OnboardingBusinessProfileUpdate(
            businessName="Cafe PA",
            country_code="PA",
            base_currency_code="USD",
        ),
    )
    assert result.data.profile.country_code == "PA"
    assert conn.tax["tax_lines"] is not None
    assert any(
        "tenant_public_profiles" in q and "America/Panama" in str(args)
        for kind, q, args in conn.queries
        if kind == "execute"
    )
