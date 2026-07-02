from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import ValidationError
from app.models.invitation import InvitationRole, SendInvitationRequest
from app.services import invitation_service, tenants_service


def _db_context(conn):
    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx


def _session(user_id, tenant_id):
    return SimpleNamespace(user_id=user_id, tenant_id=tenant_id)


def _tenant_context(tenant_id):
    return SimpleNamespace(
        tenant_id=tenant_id,
        tenant_email="team@example.com",
        site="tenant.example.com",
    )


def _request():
    request = MagicMock()
    request.headers = {
        "origin": "http://localhost:8080",
        "user-agent": "pytest",
    }
    return request


@pytest.mark.asyncio
async def test_send_invitation_allows_existing_customer_without_team_membership():
    tenant_id = uuid4()
    inviter_id = uuid4()
    customer_profile_id = uuid4()
    invitation_id = uuid4()
    expires_at = datetime.utcnow() + timedelta(days=7)

    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": tenant_id,
                "name": "Tenant",
                "slug": "tenant",
                "tenant_email": "tenant@example.com",
                "brand_name": "Tenant",
            },
            {"id": customer_profile_id, "email": "buyer@example.com"},
            {
                "id": invitation_id,
                "email": "buyer@example.com",
                "role": "admin",
                "status": "pending",
                "expires_at": expires_at,
            },
        ]
    )
    conn.fetchval = AsyncMock(side_effect=["admin", None, None, "Inviter"])
    conn.execute = AsyncMock(return_value=None)

    payload = SendInvitationRequest(
        email="buyer@example.com",
        phone="3001234567",
        name="Buyer",
        role=InvitationRole.ADMIN,
    )

    with (
        patch("app.services.invitation_service.require_valid_session", return_value=_session(inviter_id, tenant_id)),
        patch("app.services.invitation_service.require_valid_tenant", return_value=_tenant_context(tenant_id)),
        patch("app.services.invitation_service.get_db_connection", side_effect=_db_context(conn)),
        patch("app.services.aws_ses_service.ses_service") as ses_service,
    ):
        ses_service.send_email = AsyncMock(return_value=True)
        response = await invitation_service.send_invitation(_request(), payload)

    assert response.success is True
    existing_member_query = conn.fetchval.await_args_list[1].args[0]
    assert "role = ANY" in existing_member_query
    assert "is_active = true" in existing_member_query
    assert conn.fetchval.await_args_list[1].args[1:] == (
        customer_profile_id,
        tenant_id,
        ["superuser", "admin", "employee", "member", "promotor"],
    )


@pytest.mark.asyncio
async def test_accept_invitation_inserts_team_membership_without_updating_legacy_customer_row():
    tenant_id = uuid4()
    profile_id = uuid4()
    invitation_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": invitation_id,
                "tenant_id": tenant_id,
                "user_id": profile_id,
                "email": "buyer@example.com",
                "name": "Buyer",
                "role": "admin",
            },
            None,
        ]
    )
    conn.fetchval = AsyncMock(return_value="Tenant")
    conn.execute = AsyncMock(return_value=None)

    with (
        patch("app.services.invitation_service.require_valid_tenant", return_value=_tenant_context(tenant_id)),
        patch("app.services.invitation_service.get_db_connection", side_effect=_db_context(conn)),
        patch("app.services.invitation_service.set_session_cookie", new=AsyncMock()),
        patch("app.services.invitation_service.check_plan_quota_growth", new=AsyncMock()),
    ):
        response = await invitation_service.accept_invitation(_request(), MagicMock(), "token")

    assert response.success is True
    team_lookup_query = conn.fetchrow.await_args_list[1].args[0]
    assert "role = ANY" in team_lookup_query
    assert conn.fetchrow.await_args_list[1].args[1:] == (
        profile_id,
        tenant_id,
        ["superuser", "admin", "employee", "member", "promotor"],
    )

    executed_sql = [call.args[0] for call in conn.execute.await_args_list]
    tenant_member_writes = [sql for sql in executed_sql if "tenant_members" in sql]
    assert len(tenant_member_writes) == 1
    assert "INSERT INTO tenant_members" in tenant_member_writes[0]
    assert "UPDATE tenant_members" not in tenant_member_writes[0]
    assert any("replaced_by_new_login" in sql for sql in executed_sql)


@pytest.mark.asyncio
async def test_accept_invitation_reactivates_existing_team_membership_by_id():
    tenant_id = uuid4()
    profile_id = uuid4()
    invitation_id = uuid4()
    member_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": invitation_id,
                "tenant_id": tenant_id,
                "user_id": profile_id,
                "email": "employee@example.com",
                "name": "Employee",
                "role": "admin",
            },
            {"id": member_id, "is_active": False},
        ]
    )
    conn.fetchval = AsyncMock(return_value="Tenant")
    conn.execute = AsyncMock(return_value=None)

    with (
        patch("app.services.invitation_service.require_valid_tenant", return_value=_tenant_context(tenant_id)),
        patch("app.services.invitation_service.get_db_connection", side_effect=_db_context(conn)),
        patch("app.services.invitation_service.set_session_cookie", new=AsyncMock()),
        patch("app.services.invitation_service.check_plan_quota_growth", new=AsyncMock()),
    ):
        await invitation_service.accept_invitation(_request(), MagicMock(), "token")

    tenant_member_writes = [
        call.args for call in conn.execute.await_args_list
        if "tenant_members" in call.args[0]
    ]
    assert len(tenant_member_writes) == 1
    assert "WHERE id = $2" in tenant_member_writes[0][0]
    assert tenant_member_writes[0][1:] == ("admin", member_id)
    executed_sql = [call.args[0] for call in conn.execute.await_args_list]
    assert any("replaced_by_new_login" in sql for sql in executed_sql)


@pytest.mark.asyncio
async def test_update_member_role_rejects_non_team_member_row():
    tenant_id = uuid4()
    superuser_id = uuid4()
    member_id = uuid4()
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value="superuser")
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value=None)

    with (
        patch("app.services.tenants_service.require_valid_session", return_value=_session(superuser_id, tenant_id)),
        patch("app.services.tenants_service.get_db_connection", side_effect=_db_context(conn)),
    ):
        with pytest.raises(ValidationError):
            await tenants_service.update_member_role(_request(), str(member_id), "admin")

    member_lookup_query = conn.fetchrow.await_args.args[0]
    assert "tm.role = ANY" in member_lookup_query
    assert "tm.is_active = true" in member_lookup_query
    conn.execute.assert_not_awaited()
