from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models.onboarding import OnboardingBusinessProfileUpdate
from app.routers import billing
from app.services import legal_service, onboarding_service


def _profile_row(
    tenant_id, *, business_name="Cafe Central", country="US", currency="USD", revision=1
):
    colombia = country == "CO"
    return {
        "business_name": business_name,
        "profile_tenant_id": tenant_id,
        "country_code": country,
        "base_currency_code": currency,
        "accounting_localization": (
            "WARO_CO_PUC_V1" if colombia else "WARO_HOSPITALITY_GLOBAL_V1"
        ),
        "document_mode": "fiscal_integrated" if colombia else "waro_commercial",
        "fiscal_provider": "matias" if colombia else None,
        "selection_revision": revision,
        "profile_created_at": None,
        "profile_updated_at": None,
    }


def test_business_name_rejects_the_pending_placeholder():
    with pytest.raises(ValidationError):
        OnboardingBusinessProfileUpdate(
            business_name="  Negocio pendiente  ",
            country_code="CO",
            base_currency_code="COP",
        )


@pytest.mark.asyncio
async def test_catalog_read_has_no_default_profile_write():
    tenant_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "lifecycle_status": "pending",
        "state": "business_profile_pending",
        "business_name": "Negocio pendiente",
        "profile_tenant_id": None,
    })

    result = await onboarding_service.get_onboarding_financial_profile(conn, tenant_id)

    assert result.data.profile is None
    assert len(result.data.catalog) == 23
    assert result.data.next_step == "business_profile"
    query = conn.fetchrow.await_args.args[0]
    assert "INSERT" not in query
    assert "tenant_financial_profiles" in query


@pytest.mark.asyncio
async def test_initial_selection_is_atomic_and_advances_to_terms():
    tenant_id = uuid4()
    profile = _profile_row(tenant_id)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {"lifecycle_status": "pending", "state": "business_profile_pending"},
        profile,
        {"state": "terms_pending"},
    ])

    result = await onboarding_service.update_onboarding_financial_profile(
        conn,
        tenant_id,
        OnboardingBusinessProfileUpdate(
            business_name="  Cafe   Central  ",
            country_code="us",
            base_currency_code="usd",
        ),
    )

    assert result.data.profile.country_code == "US"
    assert result.data.business_name == "Cafe Central"
    assert result.data.profile.selection_revision == 1
    assert result.data.state == "terms_pending"
    assert result.data.next_step == "terms"
    lock_query = conn.fetchrow.await_args_list[0].args[0]
    upsert_query = conn.fetchrow.await_args_list[1].args[0]
    assert "FOR UPDATE OF t, o" in lock_query
    assert "ON CONFLICT (tenant_id) DO UPDATE" in upsert_query
    assert "selection_revision + 1" in upsert_query
    tenant_update = conn.execute.await_args.args[0]
    assert "UPDATE tenants" in tenant_update
    assert conn.execute.await_args.args[2] == "Cafe Central"


@pytest.mark.asyncio
async def test_idempotent_selection_keeps_database_revision():
    tenant_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {
            "lifecycle_status": "pending",
            "state": "payment_pending",
            "business_name": "Cafe Central",
        },
        _profile_row(tenant_id, revision=4),
    ])

    result = await onboarding_service.update_onboarding_financial_profile(
        conn,
        tenant_id,
        OnboardingBusinessProfileUpdate(
            business_name="Cafe Central",
            country_code="US",
            base_currency_code="USD",
        ),
    )

    assert result.data.profile.selection_revision == 4
    assert result.data.state == "payment_pending"
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_payment_pending_rejects_changes_after_legal_acceptance():
    tenant_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {
            "lifecycle_status": "pending",
            "state": "payment_pending",
            "business_name": "Cafe Central",
        },
        _profile_row(tenant_id, country="CO", currency="COP", revision=2),
    ])

    with pytest.raises(HTTPException) as exc:
        await onboarding_service.update_onboarding_financial_profile(
            conn,
            tenant_id,
            OnboardingBusinessProfileUpdate(
                business_name="Cafe Central",
                country_code="US",
                base_currency_code="USD",
            ),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == {
        "code": "ONBOARDING_FINANCIAL_PROFILE_LOCKED",
        "state": "payment_pending",
    }
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_paid_onboarding_cannot_change_financial_selection():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "lifecycle_status": "pending",
        "state": "paid",
    })

    with pytest.raises(HTTPException) as exc:
        await onboarding_service.update_onboarding_financial_profile(
            conn,
            uuid4(),
            OnboardingBusinessProfileUpdate(
                business_name="Cafe Central",
                country_code="CO",
                base_currency_code="COP",
            ),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "ONBOARDING_FINANCIAL_PROFILE_LOCKED"
    assert conn.fetchrow.await_count == 1


@pytest.mark.asyncio
async def test_pending_acceptance_requires_financial_profile():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "lifecycle_status": "pending",
        "state": "terms_pending",
        "country_code": None,
    })
    session = SimpleNamespace(tenant_id=uuid4())

    with pytest.raises(HTTPException) as exc:
        await onboarding_service.accept_onboarding_terms(
            conn, session, client_ip="203.0.113.10", user_agent="pytest"
        )

    assert exc.value.detail["code"] == "ONBOARDING_FINANCIAL_PROFILE_REQUIRED"


@pytest.mark.asyncio
async def test_pending_acceptance_uses_canonical_source_and_advances_payment():
    tenant_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {
            "lifecycle_status": "pending",
            "state": "terms_pending",
            "country_code": "CO",
        },
        {"state": "payment_pending"},
    ])
    session = SimpleNamespace(tenant_id=tenant_id)
    accepted = {"success": True, "data": {"acceptance": {"id": "evidence"}}}

    with patch.object(
        onboarding_service.legal_service,
        "accept_current_terms",
        new=AsyncMock(return_value=accepted),
    ) as accept:
        result = await onboarding_service.accept_onboarding_terms(
            conn, session, client_ip="203.0.113.10", user_agent="pytest"
        )

    assert accept.await_args.kwargs["source"] == "onboarding"
    assert result["data"]["onboarding"] == {
        "state": "payment_pending",
        "nextStep": "payment",
    }


@pytest.mark.asyncio
async def test_payment_gate_requires_profile_state_and_current_acceptance():
    tenant_id = uuid4()
    version_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "lifecycle_status": "pending",
        "state": "payment_pending",
        "profile_tenant_id": tenant_id,
    })
    session = SimpleNamespace(tenant_id=tenant_id)

    with patch.object(
        onboarding_service.legal_service,
        "get_current_terms",
        new=AsyncMock(return_value={"version_id": str(version_id)}),
    ), patch.object(
        onboarding_service.legal_service,
        "get_acceptance_for_version",
        new=AsyncMock(return_value={"id": "accepted"}),
    ):
        await onboarding_service.ensure_onboarding_payment_ready(conn, session)


@pytest.mark.asyncio
async def test_payment_gate_fails_closed_without_published_terms():
    tenant_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "lifecycle_status": "pending",
        "state": "payment_pending",
        "profile_tenant_id": tenant_id,
    })
    session = SimpleNamespace(tenant_id=tenant_id)

    with patch.object(
        onboarding_service.legal_service,
        "get_current_terms",
        new=AsyncMock(return_value=None),
    ):
        with pytest.raises(HTTPException) as exc:
            await onboarding_service.ensure_onboarding_payment_ready(conn, session)

    assert exc.value.detail["code"] == "ONBOARDING_CURRENT_TERMS_REQUIRED"


@pytest.mark.asyncio
async def test_pending_subscribe_checks_onboarding_before_wompi():
    tenant_id = uuid4()
    session = SimpleNamespace(tenant_id=tenant_id, lifecycle_status="pending")
    conn = AsyncMock()

    @asynccontextmanager
    async def db_context():
        yield conn

    not_ready = HTTPException(
        status_code=409,
        detail={"code": "ONBOARDING_PAYMENT_NOT_READY"},
    )
    with patch.object(billing, "require_valid_session", return_value=session), patch.object(
        billing, "get_db_connection", side_effect=db_context
    ), patch.object(
        billing.billing_service,
        "get_plan_for_subscribe",
        new=AsyncMock(return_value={"name": "Plan", "price_annual": 100}),
    ) as get_plan, patch.object(
        billing.onboarding_service,
        "ensure_onboarding_payment_ready",
        new=AsyncMock(side_effect=not_ready),
    ), patch.object(
        billing.wompi_service,
        "create_payment_link",
        new=AsyncMock(),
    ) as wompi:
        with pytest.raises(HTTPException) as exc:
            await billing.subscribe(
                billing.SubscribeBody(plan_id=uuid4(), billing_cycle="annual"),
                object(),
            )

    assert exc.value.detail["code"] == "ONBOARDING_PAYMENT_NOT_READY"
    get_plan.assert_not_awaited()
    wompi.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_status_includes_profile_and_current_acceptance():
    tenant_id = uuid4()
    version_id = uuid4()
    row = {
        "tenant_id": tenant_id,
        "business_name": "Cafe Central",
        "lifecycle_status": "pending",
        "state": "payment_pending",
        "email_verified_at": datetime.now(timezone.utc),
        **_profile_row(tenant_id, country="CO", currency="COP", revision=2),
    }
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=row)

    with patch.object(
        onboarding_service.legal_service,
        "get_current_terms",
        new=AsyncMock(return_value={"version_id": str(version_id), "version": "1.1"}),
    ), patch.object(
        onboarding_service.legal_service,
        "get_acceptance_for_version",
        new=AsyncMock(return_value={"id": "accepted"}),
    ):
        result = await onboarding_service.get_status_for_tenant(conn, tenant_id)

    assert result.data.financial_profile.country_code == "CO"
    assert result.data.business_name == "Cafe Central"
    assert result.data.financial_profile.selection_revision == 2
    assert result.data.terms_accepted is True
    assert result.data.terms_version == "1.1"
    assert result.data.next_step == "payment"


@pytest.mark.asyncio
async def test_terms_metadata_is_global_and_annex_query_is_country_scoped():
    version_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "document_id": uuid4(),
        "document_code": "terms_conditions",
        "document_title": "Terminos",
        "retention_years": 10,
        "version_id": version_id,
        "version": "1.1",
        "effective_at": datetime.now(timezone.utc),
        "published_at": datetime.now(timezone.utc),
        "content_url": "/terms",
        "content_sha256": "hash",
        "metadata": {},
    })
    conn.fetch = AsyncMock(return_value=[])

    current = await legal_service.get_current_terms(conn, uuid4())

    assert current["metadata"]["applicability"] == {"scope_type": "global"}
    annex_query = conn.fetch.await_args.args[0]
    assert "scope_type = 'global'" in annex_query
    assert "scope_type = 'country'" in annex_query
    assert "FROM tenant_financial_profiles" in annex_query


def test_migration_adds_revision_country_snapshot_and_global_scope():
    sql = Path("migrations/107_onboarding_country_terms.sql").read_text()
    assert "selection_revision BIGINT NOT NULL DEFAULT 1" in sql
    assert "country_code_snapshot CHAR(2)" in sql
    assert "tenant_financial_profiles_selection_revision_check" in sql
    assert "'{applicability}'" in sql
