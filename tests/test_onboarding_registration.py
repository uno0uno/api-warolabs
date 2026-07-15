from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services.onboarding_service import (
    complete_registration,
    store_registration_challenge,
)


@pytest.mark.asyncio
async def test_new_email_challenge_is_hashed_and_does_not_create_identity():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"email_count": 0, "ip_count": 0})
    conn.execute = AsyncMock(return_value="OK")

    with patch("app.services.onboarding_service.settings.auth_secret", "test-secret"):
        created = await store_registration_challenge(
            conn,
            email="new@example.com",
            token="raw-token",
            code="123456",
            request_ip="127.0.0.1",
            user_agent="pytest",
        )

    assert created is True
    insert = next(
        call for call in conn.execute.await_args_list
        if "INSERT INTO onboarding_email_challenges" in call.args[0]
    )
    assert "onboarding_email_challenges" in insert.args[0]
    assert "raw-token" not in insert.args
    assert "123456" not in insert.args
    assert len(insert.args[2]) == 64
    assert len(insert.args[3]) == 64
    sql = " ".join(call.args[0] for call in conn.execute.await_args_list)
    assert "INSERT INTO profile" not in sql
    assert "INSERT INTO tenants" not in sql


@pytest.mark.asyncio
async def test_profile_without_active_tenant_can_start_verified_onboarding():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"email_count": 0, "ip_count": 0})
    conn.execute = AsyncMock(return_value="OK")

    created = await store_registration_challenge(
        conn,
        email="customer@example.com",
        token="token",
        code="123456",
        request_ip=None,
        user_agent="pytest",
    )

    assert created is True
    assert conn.execute.await_count == 3


@pytest.mark.asyncio
async def test_registration_challenge_enforces_persisted_rate_limit():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"email_count": 5, "ip_count": 1})

    with pytest.raises(HTTPException) as exc:
        await store_registration_challenge(
            conn,
            email="new@example.com",
            token="token",
            code="123456",
            request_ip="127.0.0.1",
            user_agent="pytest",
        )

    assert exc.value.status_code == 429
    assert conn.execute.await_count == 2
    assert all(
        "pg_advisory_xact_lock" in call.args[0]
        for call in conn.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_verified_challenge_atomically_creates_pending_owner():
    challenge_id = uuid4()
    user_id = uuid4()
    tenant_id = uuid4()
    created_at = datetime.now(timezone.utc)
    verified_at = datetime.now(timezone.utc)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {
            "id": challenge_id,
            "consumed_at": None,
            "completed_user_id": None,
            "completed_tenant_id": None,
        },
        None,
        None,
        {
            "id": user_id,
            "email": "new@example.com",
            "name": None,
            "created_at": created_at,
        },
        {"state": "business_profile_pending", "email_verified_at": verified_at},
    ])
    conn.execute = AsyncMock(return_value="OK")

    with patch("app.services.onboarding_service.settings.auth_secret", "test-secret"), patch(
        "app.services.onboarding_service.uuid4", return_value=tenant_id
    ):
        identity = await complete_registration(
            conn,
            email="new@example.com",
            credential="raw-token",
            kind="token",
        )

    assert identity["user_id"] == user_id
    assert identity["tenant_id"] == tenant_id
    assert identity["lifecycle_status"] == "pending"
    assert identity["onboarding_state"] == "business_profile_pending"
    assert identity["next_step"] == "business_profile"
    writes = "\n".join(call.args[0] for call in conn.execute.await_args_list)
    assert "pg_advisory_xact_lock" in writes
    assert "INSERT INTO tenants" in writes
    assert "'pending'" in writes
    assert "INSERT INTO tenant_members" in writes
    assert "'owner', false" in writes
    assert "tenant_public_profiles" not in writes
    assert "seed_tenant_accounts" not in writes


@pytest.mark.asyncio
async def test_completed_challenge_retry_returns_same_identity_without_writes():
    challenge_id = uuid4()
    user_id = uuid4()
    tenant_id = uuid4()
    identity_row = {
        "user_id": user_id,
        "email": "new@example.com",
        "name": None,
        "user_created_at": datetime.now(timezone.utc),
        "tenant_id": tenant_id,
        "tenant_name": "Negocio pendiente",
        "tenant_slug": "onboarding-stable",
        "lifecycle_status": "pending",
        "onboarding_state": "business_profile_pending",
        "email_verified_at": datetime.now(timezone.utc),
    }
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {
            "id": challenge_id,
            "consumed_at": datetime.now(timezone.utc),
            "completed_user_id": user_id,
            "completed_tenant_id": tenant_id,
        },
        identity_row,
    ])

    with patch("app.services.onboarding_service.settings.auth_secret", "test-secret"):
        identity = await complete_registration(
            conn,
            email="new@example.com",
            credential="raw-token",
            kind="token",
        )

    assert identity["user_id"] == user_id
    assert identity["tenant_id"] == tenant_id
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_superseded_challenge_cannot_provision_identity():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "id": uuid4(),
        "consumed_at": datetime.now(timezone.utc),
        "completed_user_id": None,
        "completed_tenant_id": None,
    })

    with patch("app.services.onboarding_service.settings.auth_secret", "test-secret"):
        identity = await complete_registration(
            conn,
            email="new@example.com",
            credential="old-token",
            kind="token",
        )

    assert identity is None
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_magic_token_verification_returns_pending_session_contract():
    from app.services.magic_link_service import verify_token

    user_id = uuid4()
    tenant_id = uuid4()
    now = datetime.now(timezone.utc)
    identity = {
        "user_id": user_id,
        "email": "new@example.com",
        "name": None,
        "user_created_at": now,
        "tenant_id": tenant_id,
        "tenant_name": "Negocio pendiente",
        "tenant_slug": "onboarding-stable",
        "lifecycle_status": "pending",
        "onboarding_state": "business_profile_pending",
        "email_verified_at": now,
        "next_step": "business_profile",
    }
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="OK")

    @asynccontextmanager
    async def db_ctx():
        yield conn

    request = MagicMock()
    request.headers = {"user-agent": "pytest"}
    request.client = SimpleNamespace(host="127.0.0.1")
    response = MagicMock()
    tenant_context = SimpleNamespace(site="warocol.com")

    with patch("app.services.magic_link_service.get_db_connection", side_effect=db_ctx), patch(
        "app.services.magic_link_service.require_valid_tenant", return_value=tenant_context
    ), patch(
        "app.services.magic_link_service.complete_registration",
        new=AsyncMock(return_value=identity),
    ), patch(
        "app.services.magic_link_service.replace_active_admin_sessions",
        new=AsyncMock(return_value=0),
    ), patch(
        "app.services.magic_link_service.set_session_cookie", new=AsyncMock()
    ):
        result = await verify_token(request, response, "new@example.com", "token")

    assert result.tenant.id == tenant_id
    assert result.onboarding.lifecycle_status == "pending"
    assert result.onboarding.next_step == "business_profile"
    session_write = next(
        call for call in conn.execute.await_args_list if "INSERT INTO sessions" in call.args[0]
    )
    assert session_write.args[2] == user_id
    assert session_write.args[3] == tenant_id


def test_migration_has_database_idempotency_and_lifecycle_guards():
    sql = Path("migrations/106_onboarding_registration.sql").read_text()
    assert "profile_email_lower_unique" in sql
    assert "duplicate group(s) require identity reconciliation" in sql
    assert "tenants_lifecycle_status_check" in sql
    assert "tenant_onboarding_owner_in_progress_unique" in sql
    assert "tenant_members_pending_owner_unique" in sql
    assert "onboarding_email_challenges" in sql
    assert "DEFAULT 'active'" in sql
