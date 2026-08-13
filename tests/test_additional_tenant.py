"""Tests for authenticated additional-tenant bootstrap (api-warolabs#834)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.core.exceptions import AuthorizationError
from app.models.onboarding import OnboardingBusinessProfileUpdate
from app.services.onboarding_service import (
    MAX_ADDITIONAL_TENANT_CREATES_PER_HOUR,
    bootstrap_additional_tenant,
)


def _session(*, role="superuser", user_id=None, tenant_id=None, email="owner@example.com"):
    return SimpleNamespace(
        role=role,
        user_id=user_id or uuid4(),
        tenant_id=tenant_id or uuid4(),
        email=email,
    )


def _payload(**overrides):
    data = {
        "businessName": "Second Cafe",
        "country_code": "CO",
        "base_currency_code": "COP",
    }
    data.update(overrides)
    return OnboardingBusinessProfileUpdate(**data)


@pytest.mark.asyncio
async def test_non_superuser_forbidden():
    conn = AsyncMock()
    session = _session(role="admin")

    with pytest.raises(AuthorizationError):
        await bootstrap_additional_tenant(conn, session, _payload())

    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_db_role_must_be_superuser():
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value="admin")
    session = _session(role="superuser")

    with pytest.raises(AuthorizationError):
        await bootstrap_additional_tenant(conn, session, _payload())


@pytest.mark.asyncio
async def test_resumes_incomplete_pipeline_state():
    owner = uuid4()
    incomplete_id = uuid4()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value="superuser")
    conn.fetch = AsyncMock(
        return_value=[
            {
                "tenant_id": incomplete_id,
                "name": "Half Done",
                "slug": "half-done",
                "lifecycle_status": "active",
                "state": "payment_pending",
            }
        ]
    )

    result = await bootstrap_additional_tenant(conn, _session(user_id=owner), _payload())

    assert result.data.resumed is True
    assert result.data.tenant_id == incomplete_id
    assert result.data.slug == "half-done"
    assert result.data.state == "payment_pending"
    assert result.data.next_step == "payment"
    # Must not create another tenant when resuming.
    assert not any(
        "INSERT INTO tenants" in str(call.args[0])
        for call in conn.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_resumes_starter_without_terms_acceptance():
    owner = uuid4()
    incomplete_id = uuid4()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value="superuser")
    conn.fetch = AsyncMock(
        return_value=[
            {
                "tenant_id": incomplete_id,
                "name": "No Terms Yet",
                "slug": "no-terms-yet",
                "lifecycle_status": "active",
                "state": "starter_active",
            }
        ]
    )

    with patch(
        "app.services.onboarding_service.legal_service.has_current_terms_acceptance",
        AsyncMock(return_value=False),
    ):
        result = await bootstrap_additional_tenant(conn, _session(user_id=owner), _payload())

    assert result.data.resumed is True
    assert result.data.tenant_id == incomplete_id
    assert result.data.next_step == "setup"


@pytest.mark.asyncio
async def test_creates_when_existing_starter_has_terms():
    owner = uuid4()
    existing_id = uuid4()
    new_tenant_id = uuid4()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=["superuser", 0])
    conn.fetch = AsyncMock(
        return_value=[
            {
                "tenant_id": existing_id,
                "name": "First Biz",
                "slug": "first-biz",
                "lifecycle_status": "active",
                "state": "starter_active",
            }
        ]
    )
    conn.execute = AsyncMock(return_value="OK")
    conn.fetchrow = AsyncMock(
        return_value={
            "slug": "second-cafe",
            "name": "Second Cafe",
            "lifecycle_status": "active",
        }
    )

    financial = SimpleNamespace(
        data=SimpleNamespace(business_name="Second Cafe", state="starter_active")
    )

    with patch(
        "app.services.onboarding_service.legal_service.has_current_terms_acceptance",
        AsyncMock(return_value=True),
    ), patch(
        "app.services.onboarding_service.uuid4",
        return_value=new_tenant_id,
    ), patch(
        "app.services.onboarding_service.update_onboarding_financial_profile",
        AsyncMock(return_value=financial),
    ) as apply_financial:
        result = await bootstrap_additional_tenant(conn, _session(user_id=owner), _payload())

    assert result.data.resumed is False
    assert result.data.tenant_id == new_tenant_id
    assert result.data.slug == "second-cafe"
    assert result.data.state == "starter_active"
    apply_financial.assert_awaited_once()
    assert apply_financial.await_args.args[1] == new_tenant_id
    insert_sql = " ".join(call.args[0] for call in conn.execute.await_args_list)
    assert "INSERT INTO tenants" in insert_sql
    assert "INSERT INTO tenant_onboarding" in insert_sql
    assert "INSERT INTO tenant_members" in insert_sql
    assert "pg_advisory_xact_lock" in insert_sql


@pytest.mark.asyncio
async def test_rate_limit_blocks_burst_creates():
    owner = uuid4()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(
        side_effect=["superuser", MAX_ADDITIONAL_TENANT_CREATES_PER_HOUR]
    )
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="OK")

    with pytest.raises(HTTPException) as exc:
        await bootstrap_additional_tenant(conn, _session(user_id=owner), _payload())

    assert exc.value.status_code == 429
    assert exc.value.detail["code"] == "ADDITIONAL_TENANT_RATE_LIMITED"


@pytest.mark.asyncio
async def test_opaque_slug_conflict_propagates():
    owner = uuid4()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=["superuser", 0])
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="OK")

    with patch(
        "app.services.onboarding_service.update_onboarding_financial_profile",
        AsyncMock(
            side_effect=HTTPException(
                status_code=409,
                detail={
                    "code": "BUSINESS_IDENTITY_UNAVAILABLE",
                    "message": "Choose a different business name.",
                },
            )
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await bootstrap_additional_tenant(conn, _session(user_id=owner), _payload())

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "BUSINESS_IDENTITY_UNAVAILABLE"
