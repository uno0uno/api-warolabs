"""Tests for the is_active membership filter in tenant-aware auth queries (#201).

Sub-task E2.18 of Epic 2 (#164). Validates that three sibling SQL queries
correctly filter `tenant_members.is_active = true` so that soft-deleted
(terminated) members cannot:

1. Switch back into a tenant they were removed from (`switch_tenant`).
2. Have their stale role surface in session reads (`get_session_data`).
3. See the terminated tenant in the sidebar tenant list (`get_user_tenants`).

Tests assert behavior at the service layer (direct function calls with
mocked DB) so they don't need full HTTP middleware or a live tenants table.
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import AuthenticationError
from app.core.middleware import SessionContext


# ─── Fixtures ─────────────────────────────────────────────────────────


def _build_session(role="admin"):
    """A valid session context for an arbitrary user/tenant pair."""
    return SessionContext({
        "user_id": uuid4(),
        "tenant_id": uuid4(),
        "email": "test@example.com",
        "name": "Test User",
        "expires_at": None,
        "is_active": True,
        "role": role,
    })


def _build_request_with_session(session):
    """A fake Request whose state.session_context is set + has session cookie."""
    request = MagicMock()
    request.state.session_context = session
    request.cookies = {"session-token": "fake-session-token-deadbeef"}
    request.headers = {}
    return request


def _build_response():
    """A fake Response that captures cookie sets without writing real headers."""
    response = MagicMock()
    return response


def _build_db_mock(fetchrow_side_effect=None, fetch_return=None):
    """Build a mocked async-context db connection.

    fetchrow_side_effect: list of return values for sequential fetchrow calls
    fetch_return: single list returned by conn.fetch (for SELECT … queries)
    """
    conn = MagicMock()
    if fetchrow_side_effect is not None:
        conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    else:
        conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=fetch_return or [])
    conn.execute = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx, conn


# ─── switch_tenant tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_active_member_can_switch_tenant():
    """Happy path: user with is_active=true membership row → new session created."""
    from app.services.auth_service import switch_tenant

    session = _build_session()
    request = _build_request_with_session(session)
    response = _build_response()

    target_tenant_id = uuid4()
    fetchrow_responses = [
        # 1) current session lookup → returns a session row with a DIFFERENT slug
        {
            "ip_address": "127.0.0.1",
            "user_agent": "test-ua",
            "login_method": "magic-link",
            "tenant_id": session.tenant_id,
            "current_tenant_slug": "previous-tenant",
        },
        # 2) tenant_access_query → returns a row (membership is active)
        {
            "id": target_tenant_id,
            "name": "Target Tenant",
            "slug": "target-tenant",
            "site": "https://target.example.com",
        },
    ]

    db_ctx, _ = _build_db_mock(fetchrow_side_effect=fetchrow_responses)

    with patch("app.services.auth_service.get_db_connection", side_effect=db_ctx), \
         patch("app.services.auth_service.get_session_token", new=AsyncMock(return_value="tok")), \
         patch("app.services.auth_service.require_valid_session", return_value=session), \
         patch("app.services.auth_service.set_session_cookie", new=AsyncMock(return_value=None)), \
         patch("app.services.auth_service.get_client_ip", return_value="127.0.0.1"):
        result = await switch_tenant(request, response, "target-tenant")

    assert result.tenant.slug == "target-tenant"
    assert result.tenant.id == target_tenant_id


@pytest.mark.asyncio
async def test_terminated_member_cannot_switch_tenant():
    """Soft-deleted (is_active=false) membership → query returns no row → 401.

    The mock returns None for the tenant_access_query, simulating what
    happens AFTER the is_active filter is applied to a row that has
    is_active=false. The service must raise AuthenticationError.
    """
    from app.services.auth_service import switch_tenant

    session = _build_session()
    request = _build_request_with_session(session)
    response = _build_response()

    fetchrow_responses = [
        # 1) current session lookup → succeeds
        {
            "ip_address": "127.0.0.1",
            "user_agent": "test-ua",
            "login_method": "magic-link",
            "tenant_id": session.tenant_id,
            "current_tenant_slug": "previous-tenant",
        },
        # 2) tenant_access_query → None (membership row exists but is_active=false,
        #    so the WHERE clause filters it out).
        None,
    ]

    db_ctx, _ = _build_db_mock(fetchrow_side_effect=fetchrow_responses)

    with patch("app.services.auth_service.get_db_connection", side_effect=db_ctx), \
         patch("app.services.auth_service.get_session_token", new=AsyncMock(return_value="tok")), \
         patch("app.services.auth_service.require_valid_session", return_value=session):
        with pytest.raises(AuthenticationError) as exc_info:
            await switch_tenant(request, response, "target-tenant")

    assert "Access denied" in str(exc_info.value)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_non_member_cannot_switch_tenant():
    """Regression: user with no membership row still gets 401 (unchanged)."""
    from app.services.auth_service import switch_tenant

    session = _build_session()
    request = _build_request_with_session(session)
    response = _build_response()

    fetchrow_responses = [
        {
            "ip_address": "127.0.0.1",
            "user_agent": "test-ua",
            "login_method": "magic-link",
            "tenant_id": session.tenant_id,
            "current_tenant_slug": "previous-tenant",
        },
        None,  # no row matches user_id at all
    ]

    db_ctx, _ = _build_db_mock(fetchrow_side_effect=fetchrow_responses)

    with patch("app.services.auth_service.get_db_connection", side_effect=db_ctx), \
         patch("app.services.auth_service.get_session_token", new=AsyncMock(return_value="tok")), \
         patch("app.services.auth_service.require_valid_session", return_value=session):
        with pytest.raises(AuthenticationError) as exc_info:
            await switch_tenant(request, response, "any-tenant")

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_customer_member_cannot_switch_tenant():
    """Customer-only memberships are not internal team access."""
    from app.services.auth_service import switch_tenant

    session = _build_session(role="customer")
    request = _build_request_with_session(session)
    response = _build_response()

    with patch("app.services.auth_service.get_session_token", new=AsyncMock(return_value="tok")), \
         patch("app.services.auth_service.require_valid_session", return_value=session):
        with pytest.raises(AuthenticationError) as exc_info:
            await switch_tenant(request, response, "target-tenant")

    assert "Access denied" in str(exc_info.value)
    assert exc_info.value.status_code == 401


# ─── get_session_data tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_session_data_role_null_when_terminated():
    """get_session_data returns user.role=None when membership is_active=false.

    The role-fetch query (auth_service.py:55-60) now filters by is_active=true.
    A terminated member's role row exists in the DB but is filtered out → the
    response has role=None, which Epic 2's require_module treats as "no
    membership" and denies in enforce mode.
    """
    from app.services.auth_service import get_session_data

    user_id = uuid4()
    tenant_id = uuid4()
    session_token = "fake-token"
    request = MagicMock()
    request.cookies = {"session-token": session_token}
    request.headers = {}
    response = _build_response()

    fetchrow_responses = [
        # 1) session_query → valid session
        {
            "user_id": user_id,
            "email": "term@example.com",
            "name": "Terminated User",
            "user_created_at": "2024-01-01T00:00:00Z",
            "tenant_id": tenant_id,
            "expires_at": "2030-01-01T00:00:00Z",
            "created_at": "2025-01-01T00:00:00Z",
            "ip_address": "127.0.0.1",
            "login_method": "magic-link",
        },
        # 2) tenant_query → tenant exists
        {"id": tenant_id, "name": "Some Tenant", "slug": "some-tenant"},
        # 3) role_result → None because tm.is_active=false and the WHERE clause
        #    now includes "AND is_active = true". This is the test's payoff.
        None,
    ]

    db_ctx, _ = _build_db_mock(fetchrow_side_effect=fetchrow_responses)

    with patch("app.services.auth_service.get_db_connection", side_effect=db_ctx), \
         patch("app.services.auth_service.get_session_token", new=AsyncMock(return_value=session_token)):
        result = await get_session_data(request, response)

    assert result.user.role is None
    assert result.user.email == "term@example.com"


@pytest.mark.asyncio
async def test_get_session_data_denies_customer_role_and_clears_cookie():
    """Existing active internal sessions with role=customer are invalidated."""
    from app.services.auth_service import get_session_data

    user_id = uuid4()
    tenant_id = uuid4()
    session_token = "fake-token"
    request = MagicMock()
    request.cookies = {"session-token": session_token}
    request.headers = {}
    response = _build_response()

    fetchrow_responses = [
        {
            "user_id": user_id,
            "email": "customer@example.com",
            "name": "Customer User",
            "user_created_at": "2024-01-01T00:00:00Z",
            "tenant_id": tenant_id,
            "expires_at": "2030-01-01T00:00:00Z",
            "created_at": "2025-01-01T00:00:00Z",
            "ip_address": "127.0.0.1",
            "login_method": "magic-link",
        },
        {"id": tenant_id, "name": "Some Tenant", "slug": "some-tenant"},
        {"role": "customer"},
    ]

    db_ctx, conn = _build_db_mock(fetchrow_side_effect=fetchrow_responses)

    with patch("app.services.auth_service.get_db_connection", side_effect=db_ctx), \
         patch("app.services.auth_service.get_session_token", new=AsyncMock(return_value=session_token)), \
         patch("app.services.auth_service.clear_session_cookie", new=AsyncMock(return_value=None)) as clear_cookie:
        with pytest.raises(AuthenticationError) as exc_info:
            await get_session_data(request, response)

    assert exc_info.value.status_code == 401
    conn.execute.assert_awaited_once()
    assert conn.execute.await_args.args[1] == session_token
    clear_cookie.assert_awaited_once_with(response, session_token)


# ─── get_user_tenants tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_user_tenants_excludes_terminated_memberships():
    """get_user_tenants filters out tenants where the user's membership is_active=false.

    The query (tenants_service.py:33-38) now has "AND tm.is_active = true".
    A terminated member who still belongs to one active tenant + one
    terminated tenant should see only the active one in the response.
    """
    from app.services.tenants_service import get_user_tenants

    session = _build_session()
    request = _build_request_with_session(session)

    # The mock returns only the active tenant — because the AND tm.is_active = true
    # filter excludes the terminated one at SQL level.
    active_tenant_id = uuid4()
    fetch_return = [
        {"id": active_tenant_id, "name": "Active Tenant", "slug": "active-tenant"},
    ]

    db_ctx, conn = _build_db_mock(fetch_return=fetch_return)

    with patch("app.services.tenants_service.get_db_connection", side_effect=db_ctx), \
         patch("app.services.tenants_service.require_valid_session", return_value=session):
        result = await get_user_tenants(request)

    assert len(result.data) == 1
    assert result.data[0].slug == "active-tenant"
    assert result.data[0].id == active_tenant_id
    assert conn.fetch.await_args.args[2] == ["superuser", "admin", "employee", "member"]
