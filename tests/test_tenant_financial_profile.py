from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.tenant_prefs import (
    COUNTRY_CURRENCY_PAIRS,
    SUPPORTED_CURRENCY_MINOR_UNITS,
    validate_currency_amount,
)
from app.models.tenant_financial_profile import TenantFinancialProfileUpdate
from app.services import tenant_financial_profile_service as service


def _profile(tenant_id, country="CO", currency="COP"):
    colombia = country == "CO"
    return {
        "tenant_id": tenant_id,
        "country_code": country,
        "base_currency_code": currency,
        "accounting_localization": (
            "WARO_CO_PUC_V1" if colombia else "WARO_HOSPITALITY_GLOBAL_V1"
        ),
        "document_mode": "fiscal_integrated" if colombia else "waro_commercial",
        "fiscal_provider": "matias" if colombia else None,
        "created_at": None,
        "updated_at": None,
    }


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FakeConn:
    def __init__(self, tenant_id, blockers=None, update_row=None):
        self.tenant_id = tenant_id
        self.blockers = blockers or {
            "permanent_activity": False,
            "temporary_activity": False,
        }
        self.update_row = update_row
        self.queries = []

    def transaction(self):
        return _Transaction()

    async def execute(self, query, *args):
        self.queries.append((query, args))
        return "INSERT 0 1"

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        if "FROM tenants WHERE id" in query:
            return {"id": self.tenant_id}
        if "FROM tenant_financial_profiles" in query:
            return _profile(self.tenant_id)
        if "AS permanent_activity" in query:
            return self.blockers
        if "UPDATE tenant_financial_profiles" in query:
            return self.update_row
        raise AssertionError(f"Unexpected query: {query}")


def _db_context(conn):
    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx


def test_catalog_has_23_countries_20_currencies_and_panama_choices():
    assert len(COUNTRY_CURRENCY_PAIRS) == 23
    assert len(SUPPORTED_CURRENCY_MINOR_UNITS) == 20
    assert COUNTRY_CURRENCY_PAIRS["PA"] == ("USD", "PAB")
    assert SUPPORTED_CURRENCY_MINOR_UNITS["CLP"] == 0


def test_country_currency_model_normalizes_and_rejects_invalid_pairs():
    assert TenantFinancialProfileUpdate(
        country_code=" pa ", base_currency_code="pab"
    ).model_dump() == {"country_code": "PA", "base_currency_code": "PAB"}
    with pytest.raises(ValidationError, match="base_currency_code for CO"):
        TenantFinancialProfileUpdate(country_code="CO", base_currency_code="USD")


def test_clp_rejects_fractional_amounts_but_two_minor_unit_currency_accepts_them():
    assert validate_currency_amount("1200", "CLP") == 1200
    with pytest.raises(ValueError, match="CLP supports 0"):
        validate_currency_amount("1200.50", "CLP")
    assert validate_currency_amount("12.50", "USD") == pytest.approx(12.5)


def test_permanent_blocker_wins_and_reason_does_not_expose_rows():
    eligibility = service._eligibility(
        {"permanent_activity": True, "temporary_activity": True}
    )
    assert eligibility.lock_type == "permanent"
    assert eligibility.reason_codes == [service.PERMANENT_REASON]
    assert eligibility.model_dump() == {
        "eligible": False,
        "lock_type": "permanent",
        "reason_codes": ["PERMANENT_FINANCIAL_ACTIVITY"],
    }


def test_temporary_activity_uses_distinct_reversible_lock():
    eligibility = service._eligibility(
        {"permanent_activity": False, "temporary_activity": True}
    )
    assert eligibility.model_dump() == {
        "eligible": False,
        "lock_type": "temporary",
        "reason_codes": ["TEMPORARY_OPERATIONAL_ACTIVITY"],
    }


@pytest.mark.asyncio
async def test_update_rejects_country_change_when_eligible_hard_lock():
    tenant_id = uuid4()
    conn = FakeConn(tenant_id)
    request = object()
    session = SimpleNamespace(tenant_id=tenant_id)

    with patch.object(service, "require_valid_session", return_value=session), patch.object(
        service, "get_db_connection", side_effect=_db_context(conn)
    ):
        with pytest.raises(HTTPException) as exc:
            await service.update_financial_profile(
                request,
                TenantFinancialProfileUpdate(country_code="US", base_currency_code="USD"),
            )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "FINANCIAL_PROFILE_LOCKED"
    assert exc.value.detail["lock_type"] == "configured"
    assert exc.value.detail["reason_codes"] == [service.CONFIGURED_REASON]
    assert not any("UPDATE tenant_financial_profiles" in q for q, _ in conn.queries)


@pytest.mark.asyncio
async def test_financial_mode_derives_hospitality_for_non_co():
    localization, document_mode, fiscal_provider = service._financial_mode("US")
    assert localization == "WARO_HOSPITALITY_GLOBAL_V1"
    assert document_mode == "waro_commercial"
    assert fiscal_provider is None
    assert service._financial_mode("CO") == ("WARO_CO_PUC_V1", "fiscal_integrated", "matias")


@pytest.mark.asyncio
async def test_permanent_lock_rejects_change_but_allows_idempotent_retry():
    tenant_id = uuid4()
    blockers = {"permanent_activity": True, "temporary_activity": False}
    session = SimpleNamespace(tenant_id=tenant_id)

    locked_conn = FakeConn(tenant_id, blockers=blockers)
    with patch.object(service, "require_valid_session", return_value=session), patch.object(
        service, "get_db_connection", side_effect=_db_context(locked_conn)
    ):
        with pytest.raises(HTTPException) as exc:
            await service.update_financial_profile(
                object(),
                TenantFinancialProfileUpdate(country_code="US", base_currency_code="USD"),
            )
    assert exc.value.status_code == 409
    assert exc.value.detail["lock_type"] == "permanent"

    retry_conn = FakeConn(tenant_id, blockers=blockers)
    with patch.object(service, "require_valid_session", return_value=session), patch.object(
        service, "get_db_connection", side_effect=_db_context(retry_conn)
    ):
        retry = await service.update_financial_profile(
            object(),
            TenantFinancialProfileUpdate(country_code="CO", base_currency_code="COP"),
        )
    assert retry.profile.base_currency_code == "COP"
    assert retry.eligibility.lock_type == "permanent"


def test_migration_is_idempotent_and_does_not_rewrite_history():
    sql = Path("migrations/103_tenant_financial_profiles.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS tenant_financial_profiles" in sql
    assert "CREATE INDEX IF NOT EXISTS" in sql
    assert "ON CONFLICT (tenant_id) DO NOTHING" in sql
    for table in ("orders", "order_payments", "tenant_journal_entries", "electronic_invoices"):
        assert f"UPDATE {table}" not in sql


def test_blocker_query_is_tenant_scoped_and_ignores_null_legacy_rows():
    assert service._BLOCKERS_QUERY.count("tenant_id = $1") == 9
    assert "tenant_id IS NULL" not in service._BLOCKERS_QUERY


def test_financial_profile_read_is_not_owner_module_gated():
    from app.routers.tenant_config import router

    routes = {
        (next(iter(route.methods)), route.path): route
        for route in router.routes
        if getattr(route, "methods", None) and route.path == "/financial-profile"
    }
    assert routes[("GET", "/financial-profile")].dependant.dependencies == []
    assert len(routes[("PUT", "/financial-profile")].dependant.dependencies) == 1
