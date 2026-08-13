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
            consent=True,
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
        consent=True,
    )

    assert created is True
    assert conn.execute.await_count == 4


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
            consent=True,
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
            "normalized_email": "new@example.com",
            "consumed_at": None,
            "completed_user_id": None,
            "completed_tenant_id": None,
            "phone_country_code": 57,
            "phone_number": "3001234567",
            "business_name": "Restaurante Nuevo",
            "country_code": "CO",
            "base_currency_code": "COP",
            "first_source": "home",
            "first_content": "hero",
            "first_campaign": "launch",
            "first_variant": "a",
            "last_source": "blog",
            "last_content": "cta",
            "last_campaign": "launch",
            "last_variant": "b",
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
        {"id": uuid4()},
        {"id": uuid4()},
        {"slug": "restaurante-nuevo"},
    ])
    conn.execute = AsyncMock(return_value="OK")

    financial = AsyncMock(return_value=SimpleNamespace(data=SimpleNamespace(
        business_name="Restaurante Nuevo",
        state="starter_active",
        next_step="setup",
    )))
    with patch("app.services.onboarding_service.settings.auth_secret", "test-secret"), patch(
        "app.services.onboarding_service.uuid4", return_value=tenant_id
    ), patch(
        "app.services.onboarding_service.update_onboarding_financial_profile", financial
    ):
        identity = await complete_registration(
            conn,
            email="new@example.com",
            credential="raw-token",
            kind="token",
        )

    assert identity["user_id"] == user_id
    assert identity["tenant_id"] == tenant_id
    assert identity["lifecycle_status"] == "active"
    assert identity["tenant_name"] == "Restaurante Nuevo"
    assert identity["onboarding_state"] == "starter_active"
    # financial mock owns name→slug assignment; identity may keep provisional until real apply
    assert identity["tenant_slug"].startswith("onboarding-") or identity["tenant_slug"] == "restaurante-nuevo"
    assert identity["next_step"] == "setup"
    financial.assert_awaited_once()
    assert identity["registration_notification"]["source"] == "blog"
    assert identity["registration_notification"]["variant"] == "b"
    writes = "\n".join(call.args[0] for call in conn.execute.await_args_list)
    assert "pg_advisory_xact_lock" in writes
    assert "INSERT INTO tenants" in writes
    assert "'pending'" in writes
    assert "INSERT INTO tenant_members" in writes
    assert "'superuser', false" in writes
    assert "UPDATE profile" in writes
    assert "billing_payment_attempts" not in writes
    assert "tenant_subscriptions" not in writes
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


@pytest.mark.asyncio
async def test_apply_onboarding_locales_from_country_sets_profile_and_tenant():
    from app.services.onboarding_service import apply_onboarding_locales_from_country

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="OK")
    user_id = uuid4()
    tenant_id = uuid4()

    locale = await apply_onboarding_locales_from_country(
        conn,
        tenant_id=tenant_id,
        country_code="US",
        currency_code="USD",
        user_id=user_id,
    )
    assert locale == "en"
    assert conn.execute.await_count == 2
    profile_sql, profile_user, profile_locale = conn.execute.await_args_list[0].args[:3]
    assert "UPDATE profile" in profile_sql
    assert "preferred_locale" in profile_sql
    assert profile_user == user_id
    assert profile_locale == "en"
    tenant_sql, tenant_arg, ui_locale, receipt_locale, country, currency = (
        conn.execute.await_args_list[1].args[:6]
    )
    assert "tenant_public_profiles" in tenant_sql
    assert "ui_locale" in tenant_sql
    assert "country" in tenant_sql
    assert "currency_code" in tenant_sql
    assert tenant_arg == tenant_id
    assert ui_locale == "en"
    assert receipt_locale == "en"
    assert country == "United States"
    assert currency == "USD"

    conn.execute.reset_mock()
    locale_co = await apply_onboarding_locales_from_country(
        conn,
        tenant_id=tenant_id,
        country_code="CO",
        currency_code="COP",
        user_id=user_id,
    )
    assert locale_co == "es"
    assert conn.execute.await_args_list[0].args[2] == "es"
    assert conn.execute.await_args_list[1].args[2] == "es"
    assert conn.execute.await_args_list[1].args[4] == "Colombia"
    assert conn.execute.await_args_list[1].args[5] == "COP"


@pytest.mark.asyncio
async def test_financial_profile_create_applies_locales_from_country():
    """Wiring: first financial apply awaits locale helper with owner + country."""
    from app.models.onboarding import OnboardingBusinessProfileUpdate
    from app.services.onboarding_service import update_onboarding_financial_profile

    tenant_id = uuid4()
    owner_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {
            "lifecycle_status": "pending",
            "state": "business_profile_pending",
            "business_name": "Negocio pendiente",
            "owner_user_id": owner_id,
        },
        {
            "profile_tenant_id": tenant_id,
            "country_code": "CO",
            "base_currency_code": "COP",
            "accounting_localization": "WARO_CO_PUC_V1",
            "document_mode": "fiscal_integrated",
            "fiscal_provider": "matias",
            "selection_revision": 1,
            "profile_created_at": None,
            "profile_updated_at": None,
        },
        {"state": "starter_active"},
    ])
    conn.execute = AsyncMock(return_value="OK")

    apply_locales = AsyncMock(return_value="es")
    with patch(
        "app.services.onboarding_service.financial_service.seed_tenant_accounts",
        new=AsyncMock(),
    ), patch(
        "app.services.onboarding_service.ensure_wave1_tax_pack",
        new=AsyncMock(),
    ), patch(
        "app.services.onboarding_service.seed_tenant_timezone_from_country",
        new=AsyncMock(return_value="America/Bogota"),
    ), patch(
        "app.services.onboarding_service.apply_onboarding_locales_from_country",
        new=apply_locales,
    ), patch(
        "app.services.onboarding_service.assign_name_based_storefront_slug",
        new=AsyncMock(return_value="cafe-central"),
    ), patch(
        "app.services.onboarding_service._promote_onboarding_identity",
        new=AsyncMock(return_value="starter_active"),
    ):
        await update_onboarding_financial_profile(
            conn,
            tenant_id,
            OnboardingBusinessProfileUpdate(
                business_name="Cafe Central",
                country_code="CO",
                base_currency_code="COP",
            ),
        )

    apply_locales.assert_awaited_once_with(
        conn,
        tenant_id=tenant_id,
        country_code="CO",
        currency_code="COP",
        user_id=owner_id,
    )


@pytest.mark.asyncio
async def test_starter_active_idempotent_skips_locale_rewrite():
    """Idempotent financial retry must not re-apply locales."""
    from app.models.onboarding import OnboardingBusinessProfileUpdate
    from app.services.onboarding_service import update_onboarding_financial_profile

    tenant_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {
            "lifecycle_status": "active",
            "state": "starter_active",
            "business_name": "Cafe Central",
            "owner_user_id": uuid4(),
        },
        {
            "profile_tenant_id": tenant_id,
            "country_code": "US",
            "base_currency_code": "USD",
            "accounting_localization": "WARO_HOSPITALITY_GLOBAL_V1",
            "document_mode": "waro_commercial",
            "fiscal_provider": None,
            "selection_revision": 4,
            "profile_created_at": None,
            "profile_updated_at": None,
        },
        {"state": "starter_active"},
    ])
    conn.execute = AsyncMock(side_effect=["UPDATE 1", "UPDATE 1"])

    apply_locales = AsyncMock(return_value="en")
    with patch(
        "app.services.onboarding_service.apply_onboarding_locales_from_country",
        new=apply_locales,
    ):
        result = await update_onboarding_financial_profile(
            conn,
            tenant_id,
            OnboardingBusinessProfileUpdate(
                business_name="Cafe Central",
                country_code="US",
                base_currency_code="USD",
                tax_jurisdiction_code="US-FL",
            ),
        )

    assert result.data.profile.selection_revision == 4
    apply_locales.assert_not_awaited()
    assert conn.execute.await_count == 2
