from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.models.auth import RegistrationMagicLinkRequest
from app.models.auth import Tenant, User
from app.models.onboarding import OnboardingStatus
from app.services.magic_link_service import (
    send_magic_link,
    send_registration_magic_link,
    verify_registration_token,
)
from app.services.onboarding_service import complete_registration, store_registration_challenge
from app.routers.auth import registration_options


def _request():
    request = MagicMock()
    request.headers = {"origin": "https://warocol.com", "user-agent": "pytest"}
    request.client = SimpleNamespace(host="127.0.0.1")
    request.state = SimpleNamespace()
    return request


def _tenant():
    return SimpleNamespace(
        site="warocol.com",
        tenant_name="WARO Colombia",
        tenant_id=uuid4(),
        tenant_email="soporte@warolabs.com",
        brand_name="WARO",
    )


def _payload(**overrides):
    data = {
        "email": "nuevo@example.com",
        "phone_country_code": 57,
        "phone_number": "300 123 4567",
        "consent": True,
        "business_name": "Restaurante Prueba",
        "country_code": "CO",
        "base_currency_code": "COP",
        "source": "home",
        "content": "hero",
        "campaign": "trial-launch",
        "variant": "a",
    }
    data.update(overrides)
    return RegistrationMagicLinkRequest(**data)


@pytest.mark.asyncio
async def test_unknown_login_is_generic_and_has_no_side_effects():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)

    @asynccontextmanager
    async def db_ctx(**_kwargs):
        yield conn

    ses = AsyncMock(return_value=True)
    with patch("app.services.magic_link_service.get_db_connection", side_effect=db_ctx), patch(
        "app.services.magic_link_service.require_valid_tenant", return_value=_tenant()
    ), patch("app.services.aws_ses_service.ses_service.send_email", ses):
        result = await send_magic_link(_request(), "missing@example.com")

    assert result.success is True
    conn.execute.assert_not_awaited()
    ses.assert_not_awaited()


@pytest.mark.asyncio
async def test_unverified_registration_login_reissues_consented_challenge():
    draft = {
        "phone_country_code": 57,
        "phone_number": "3001234567",
        "business_name": "Restaurante Prueba",
        "country_code": "CO",
        "base_currency_code": "COP",
        "first_source": "home",
        "last_source": "login",
    }
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, draft])

    @asynccontextmanager
    async def db_ctx(**_kwargs):
        yield conn

    issue = AsyncMock()
    with patch("app.services.magic_link_service.get_db_connection", side_effect=db_ctx), patch(
        "app.services.magic_link_service.require_valid_tenant", return_value=_tenant()
    ), patch("app.services.magic_link_service._issue_registration_challenge", issue):
        result = await send_magic_link(_request(), "nuevo@example.com")

    assert result.success is True
    issue.assert_awaited_once()
    assert issue.await_args.kwargs["email"] == "nuevo@example.com"
    assert issue.await_args.kwargs["draft"] == draft
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_registration_link_is_opaque_and_challenge_contains_no_raw_secret():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[None, {"email_count": 0, "ip_count": 0}])
    conn.execute = AsyncMock(return_value="OK")

    @asynccontextmanager
    async def db_ctx(**_kwargs):
        yield conn

    deliver = AsyncMock()
    with patch("app.services.magic_link_service.get_db_connection", side_effect=db_ctx), patch(
        "app.services.magic_link_service.require_valid_tenant", return_value=_tenant()
    ), patch("app.services.magic_link_service._deliver_magic_link", deliver), patch(
        "app.services.onboarding_service.settings.auth_secret", "test-secret"
    ):
        await send_registration_magic_link(_request(), _payload())

    link = deliver.await_args.kwargs["magic_link_url"]
    assert "purpose=registration" in link
    assert "email=" not in link
    insert = next(
        call for call in conn.execute.await_args_list
        if "INSERT INTO onboarding_email_challenges" in call.args[0]
    )
    assert "registration" in insert.args[0]
    assert all("3001234567" != value for value in insert.args[1:4])
    assert len(insert.args[2]) == 64
    assert len(insert.args[3]) == 64
    assert len(insert.args[4]) == 64


@pytest.mark.asyncio
async def test_existing_identity_registration_redirects_to_login_without_sending_code():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={
        "user_id": uuid4(),
        "tenant_id": uuid4(),
        "role": "owner",
    })

    @asynccontextmanager
    async def db_ctx(**_kwargs):
        yield conn

    with patch("app.services.magic_link_service.get_db_connection", side_effect=db_ctx), patch(
        "app.services.magic_link_service.require_valid_tenant", return_value=_tenant()
    ), patch("app.services.magic_link_service.send_magic_link", new=AsyncMock()) as login:
        result = await send_registration_magic_link(_request(), _payload())

    assert result.action == "login_required"
    login.assert_not_awaited()
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_registration_lookup_only_accepts_pre_deploy_challenges():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="OK")

    with patch("app.services.onboarding_service.settings.auth_secret", "test-secret"):
        result = await complete_registration(
            conn,
            email="legacy@example.com",
            credential="legacy-token",
            kind="token",
            legacy_only=True,
        )

    assert result is None
    query = conn.fetchrow.await_args.args[0]
    assert "purpose = 'registration'" in query
    assert "opaque_token_hash IS NULL" in query


@pytest.mark.asyncio
async def test_registration_verifier_uses_token_only_and_dispatches_once_after_db_scope():
    conn = AsyncMock()

    @asynccontextmanager
    async def db_ctx(**_kwargs):
        yield conn

    request = _request()
    user = User(id=uuid4(), email="nuevo@example.com", name=None, createdAt="2026-07-15T00:00:00Z")
    tenant = Tenant(id=uuid4(), name="Negocio pendiente", slug="onboarding-test")
    onboarding = OnboardingStatus(
        tenantId=tenant.id,
        lifecycleStatus="pending",
        state="business_profile_pending",
        nextStep="business_profile",
        businessName=tenant.name,
    )

    async def complete(*_args, **kwargs):
        assert kwargs["email"] is None
        assert kwargs["opaque_token"] is True
        request.state.registration_notification = {
            "email": "nuevo@example.com",
            "phone": "3001234567",
            "phone_country_code": 57,
            "tenant_name": None,
            "status": "business_profile_pending",
            "source": "home",
            "content": "hero",
            "campaign": "trial",
            "variant": "a",
        }
        return user, tenant, onboarding

    create_task = MagicMock()
    notification = MagicMock(return_value=object())
    with patch("app.services.magic_link_service.get_db_connection", side_effect=db_ctx), patch(
        "app.services.magic_link_service.require_valid_tenant", return_value=_tenant()
    ), patch(
        "app.services.magic_link_service._complete_registration_login", side_effect=complete
    ), patch("app.services.magic_link_service.asyncio.create_task", create_task), patch(
        "app.services.leads_service.notify_self_service_registration", notification
    ):
        result = await verify_registration_token(request, MagicMock(), "a" * 64)

    assert result.tenant.id == tenant.id
    assert result.registration_attribution.model_dump() == {
        "source": "home",
        "content": "hero",
        "campaign": "trial",
        "variant": "a",
    }
    notification.assert_called_once()
    create_task.assert_called_once()
    assert request.state.registration_notification is None


def test_registration_payload_rejects_missing_consent_and_pii_attribution():
    with pytest.raises(ValidationError):
        _payload(consent=False)
    with pytest.raises(ValidationError):
        _payload(source="https://example.com/?email=user@example.com")
    with pytest.raises(ValidationError):
        _payload(country_code="PA", base_currency_code="COP")
    with pytest.raises(ValidationError):
        _payload(phone_country_code=999)


@pytest.mark.asyncio
async def test_registration_options_are_public_and_server_owned():
    result = await registration_options()
    colombia = next(item for item in result["catalog"] if item["country_code"] == "CO")
    assert colombia["currency_codes"] == ["COP"]
    colombia_phone = next(
        item for item in result["phone_countries"] if item["country_code"] == "CO"
    )
    assert colombia_phone["calling_code"] == 57
    assert len(result["phone_countries"]) == len(result["catalog"])


@pytest.mark.asyncio
async def test_registration_challenge_service_requires_consent_before_writing():
    conn = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await store_registration_challenge(
            conn,
            email="nuevo@example.com",
            token="token",
            code="123456",
            request_ip=None,
            user_agent="pytest",
        )

    assert getattr(exc.value, "status_code", None) == 422
    conn.execute.assert_not_awaited()
