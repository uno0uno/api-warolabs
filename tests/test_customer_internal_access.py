from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import AuthenticationError


def _build_db_mock(fetchrow_side_effect=None):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx, conn


def _tenant_context(tenant_id):
    return MagicMock(
        tenant_id=tenant_id,
        tenant_name="Tijuana Cafe Bar",
        tenant_email="admin@example.com",
        site="warocol.com",
        brand_name="WARO",
    )


def _request():
    request = MagicMock()
    request.headers = {"origin": "http://localhost:8080", "user-agent": "pytest"}
    request.client = MagicMock(host="127.0.0.1")
    return request


def _session_row(session_token, tenant_id, user_id, expires_at, role_email):
    return {
        "id": session_token,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "email": role_email,
        "name": "Session User",
        "user_created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "expires_at": expires_at,
        "created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "ip_address": None,
        "login_method": "magic_link",
    }


@pytest.mark.asyncio
async def test_send_magic_link_denies_customer_only_membership():
    """A customer-only row is filtered out before an internal magic token is created."""
    from app.services.magic_link_service import send_magic_link

    tenant_id = uuid4()
    db_ctx, conn = _build_db_mock(fetchrow_side_effect=[None])

    with patch("app.services.magic_link_service.get_db_connection", side_effect=db_ctx), \
         patch("app.services.magic_link_service.require_valid_tenant", return_value=_tenant_context(tenant_id)):
        with pytest.raises(AuthenticationError):
            await send_magic_link(_request(), "customer@example.com")

    conn.execute.assert_not_awaited()
    assert conn.fetchrow.await_args.args[1] == "customer@example.com"
    assert conn.fetchrow.await_args.args[2] == ["superuser", "admin", "employee", "member", "promotor"]


@pytest.mark.asyncio
async def test_verify_token_denies_customer_only_membership_before_session_creation():
    """Legacy customer tokens cannot be exchanged for internal sessions."""
    from app.services.magic_link_service import verify_token

    tenant_id = uuid4()
    response = MagicMock()
    db_ctx, conn = _build_db_mock(fetchrow_side_effect=[None])

    with patch("app.services.magic_link_service.get_db_connection", side_effect=db_ctx), \
         patch("app.services.magic_link_service.require_valid_tenant", return_value=_tenant_context(tenant_id)):
        with pytest.raises(AuthenticationError):
            await verify_token(_request(), response, "customer@example.com", "token")

    conn.execute.assert_not_awaited()
    assert conn.fetchrow.await_args.args[3] == ["superuser", "admin", "employee", "member", "promotor"]


@pytest.mark.asyncio
async def test_verify_token_replaces_previous_active_admin_sessions():
    """A successful admin magic-link login leaves only the newly created DB session active."""
    from app.services.magic_link_service import verify_token

    tenant_id = uuid4()
    user_id = uuid4()
    session_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    response = MagicMock()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "email": "admin@example.com",
                "name": "Admin User",
                "user_created_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "user_role": "admin",
            },
            {"name": "Tenant"},
        ]
    )
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="UPDATE 1")

    @asynccontextmanager
    async def db_ctx():
        yield conn

    with patch("app.services.magic_link_service.get_db_connection", side_effect=db_ctx), \
         patch("app.services.magic_link_service.require_valid_tenant", return_value=_tenant_context(tenant_id)), \
         patch("app.services.magic_link_service.secrets.token_hex", return_value=session_id), \
         patch("app.services.magic_link_service.set_session_cookie", new=AsyncMock()), \
         patch("app.services.discord_service.discord_session_service", None):
        result = await verify_token(_request(), response, "admin@example.com", "token")

    assert result.success is True
    replacement_calls = [
        call.args for call in conn.execute.await_args_list
        if "replaced_by_new_login" in call.args[0]
    ]
    assert len(replacement_calls) == 1
    assert replacement_calls[0][1:] == (user_id, session_id)


@pytest.mark.asyncio
async def test_auth_session_reports_internal_access_for_promotor():
    """Promotor is a valid internal role and gets an explicit allow flag."""
    from app.services.auth_service import get_session_data

    session_token = str(uuid4())
    tenant_id = uuid4()
    user_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(days=1)
    db_ctx, conn = _build_db_mock(fetchrow_side_effect=[
        _session_row(session_token, tenant_id, user_id, expires_at, "promotor@example.com"),
        {"id": tenant_id, "name": "Tijuana Cafe Bar", "slug": "tijuana-cafe-bar"},
        {"role": "promotor"},
    ])

    with patch("app.services.auth_service.get_db_connection", side_effect=db_ctx), \
         patch("app.services.auth_service.get_session_token", new=AsyncMock(return_value=session_token)):
        result = await get_session_data(_request(), MagicMock())

    assert result.has_internal_access is True
    assert result.user.role == "promotor"
    assert result.current_tenant.slug == "tijuana-cafe-bar"
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_auth_session_denies_customer_role_before_payload():
    """Customer-only internal sessions keep being invalidated instead of returning an allow payload."""
    from app.services.auth_service import get_session_data

    session_token = str(uuid4())
    tenant_id = uuid4()
    user_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(days=1)
    response = MagicMock()
    db_ctx, conn = _build_db_mock(fetchrow_side_effect=[
        _session_row(session_token, tenant_id, user_id, expires_at, "customer@example.com"),
        {"id": tenant_id, "name": "Tijuana Cafe Bar", "slug": "tijuana-cafe-bar"},
        {"role": "customer"},
    ])

    with patch("app.services.auth_service.get_db_connection", side_effect=db_ctx), \
         patch("app.services.auth_service.get_session_token", new=AsyncMock(return_value=session_token)), \
         patch("app.services.auth_service.clear_session_cookie", new=AsyncMock()) as clear_cookie:
        with pytest.raises(AuthenticationError):
            await get_session_data(_request(), response)

    conn.execute.assert_awaited_once()
    assert conn.execute.await_args.args[1] == session_token
    clear_cookie.assert_awaited_once_with(response, session_token)


@pytest.mark.asyncio
async def test_session_resolver_invalidates_customer_internal_session():
    """Middleware session resolution denies active sessions whose resolved role is customer."""
    from app.core.security import get_session_from_request

    session_token = str(uuid4())
    tenant_id = uuid4()
    user_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(days=1)
    db_ctx, conn = _build_db_mock(fetchrow_side_effect=[
        {"id": session_token, "expires_at": expires_at, "is_active": True, "ended_at": None},
        {
            "user_id": user_id,
            "tenant_id": tenant_id,
            "expires_at": expires_at,
            "is_active": True,
            "email": "customer@example.com",
            "name": "Customer User",
            "role": "customer",
        },
    ])

    with patch("app.database.get_db_connection", side_effect=db_ctx), \
         patch("app.core.security.get_session_token", new=AsyncMock(return_value=session_token)):
        result = await get_session_from_request(_request())

    assert result is None
    conn.execute.assert_awaited_once()
    assert conn.execute.await_args.args[1] == session_token


@pytest.mark.asyncio
async def test_session_resolver_replaced_session_returns_invalid_without_overwriting_reason():
    """A replaced token follows the normal invalid-session path and keeps its audit reason."""
    from app.core.security import get_session_from_request

    session_token = str(uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(days=1)
    db_ctx, conn = _build_db_mock(fetchrow_side_effect=[
        {
            "id": session_token,
            "expires_at": expires_at,
            "is_active": False,
            "ended_at": datetime.now(timezone.utc),
        },
    ])

    with patch("app.database.get_db_connection", side_effect=db_ctx), \
         patch("app.core.security.get_session_token", new=AsyncMock(return_value=session_token)):
        result = await get_session_from_request(_request())

    assert result is None
    conn.execute.assert_not_awaited()
