from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services import customers_service, tenants_service


MIGRATION_SQL = Path(__file__).resolve().parents[1] / "sql" / "20260613_tenant_customers.sql"


def _session(tenant_id, user_id=None):
    session = MagicMock()
    session.tenant_id = tenant_id
    session.user_id = user_id or uuid4()
    return session


def _request():
    request = MagicMock()
    request.headers = {"user-agent": "pytest"}
    request.client = MagicMock(host="127.0.0.1")
    return request


def _profile_row(profile_id, name="Karen Tijuana", email="karen@example.com"):
    return {
        "id": profile_id,
        "phone_number": "3001234567",
        "name": name,
        "email": email,
        "fiscal_id_type": None,
        "fiscal_id": None,
        "fiscal_business_name": None,
        "fiscal_email": None,
        "created_at": datetime(2026, 6, 13, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 6, 13, tzinfo=timezone.utc),
    }


def _db_context(conn):
    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx


def test_migration_backfills_activity_and_invalidates_customer_role_sessions():
    sql = MIGRATION_SQL.read_text()

    assert "CREATE TABLE IF NOT EXISTS tenant_customers" in sql
    assert "FROM tenant_members tm" in sql
    assert "tm.role = 'customer'" in sql
    assert "FROM orders o" in sql
    assert "FROM customer_wallet_balances cwb" in sql
    assert "FROM customer_wallet_movements cwm" in sql
    assert "FROM waros_wallets ww" in sql
    assert "FROM waros_transactions wt" in sql
    assert "FROM online_carts oc" in sql
    assert "UPDATE sessions s" in sql
    assert "end_reason = 'customer_role_denied'" in sql


@pytest.mark.asyncio
async def test_karen_tijuana_customer_admin_coexistence_stays_visible_in_customer_and_team_flows():
    tenant_id = uuid4()
    profile_id = uuid4()
    team_member_id = uuid4()

    customer_conn = MagicMock()
    customer_conn.fetchrow = AsyncMock(return_value=_profile_row(profile_id))

    with patch(
        "app.services.customers_service.require_valid_session",
        return_value=_session(tenant_id),
    ), patch(
        "app.services.customers_service.get_db_connection",
        side_effect=_db_context(customer_conn),
    ):
        customer = await customers_service.get_customer_by_id(_request(), profile_id)

    assert customer.data.id == profile_id
    customer_query = customer_conn.fetchrow.await_args.args[0]
    assert "tenant_customers tc" in customer_query
    assert "tenant_members" not in customer_query
    assert "role = 'customer'" not in customer_query

    team_conn = MagicMock()
    team_conn.fetch = AsyncMock(
        side_effect=[
            [
                {
                    "id": team_member_id,
                    "tenant_id": tenant_id,
                    "user_id": profile_id,
                    "role": "admin",
                    "profile_id": profile_id,
                    "name": "Karen Tijuana",
                    "user_name": None,
                    "email": "karen@example.com",
                    "logo_avatar": None,
                }
            ],
            [],
        ]
    )

    with patch(
        "app.services.tenants_service.require_valid_session",
        return_value=_session(tenant_id),
    ), patch(
        "app.services.tenants_service.get_db_connection",
        side_effect=_db_context(team_conn),
    ):
        members = await tenants_service.get_tenant_members(_request())

    assert members.data[0].user_id == profile_id
    assert members.data[0].role == "admin"
    team_query = team_conn.fetch.await_args_list[0].args[0]
    assert "tm.role IN ('superuser', 'admin', 'employee', 'member', 'promotor')" in team_query
    assert "tm.role = 'customer'" not in team_query


@pytest.mark.asyncio
async def test_karen_tijuana_admin_customer_session_resolves_internal_role():
    from app.core.security import get_session_from_request

    session_token = str(uuid4())
    tenant_id = uuid4()
    profile_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(days=1)

    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": session_token,
                "expires_at": expires_at,
                "last_activity_at": expires_at,
                "is_active": True,
                "ended_at": None,
            },
            {
                "user_id": profile_id,
                "tenant_id": tenant_id,
                "expires_at": expires_at,
                "is_active": True,
                "email": "karen@example.com",
                "name": "Karen Tijuana",
                "role": "admin",
            },
        ]
    )
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value=None)

    with patch("app.database.get_db_connection", side_effect=_db_context(conn)), patch(
        "app.core.security.get_session_token",
        new=AsyncMock(return_value=session_token),
    ), patch("app.core.security.touch_session_activity", new=AsyncMock()):
        result = await get_session_from_request(_request())

    assert result["role"] == "admin"
    session_query = conn.fetchrow.await_args_list[1].args[0]
    assert "LEFT JOIN LATERAL" in session_query
    assert "ORDER BY CASE WHEN tm.role = ANY($2::text[])" in session_query
    assert conn.fetchrow.await_args_list[1].args[2] == [
        "superuser",
        "admin",
        "employee",
        "member",
        "promotor",
    ]
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_karen_tijuana_legacy_customer_role_session_is_denied_after_migration():
    from app.core.security import get_session_from_request

    session_token = str(uuid4())
    tenant_id = uuid4()
    profile_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(days=1)

    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": session_token,
                "expires_at": expires_at,
                "last_activity_at": expires_at,
                "is_active": True,
                "ended_at": None,
            },
            {
                "user_id": profile_id,
                "tenant_id": tenant_id,
                "expires_at": expires_at,
                "is_active": True,
                "email": "karen@example.com",
                "name": "Karen Tijuana",
                "role": "customer",
            },
        ]
    )
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value=None)

    with patch("app.database.get_db_connection", side_effect=_db_context(conn)), patch(
        "app.core.security.get_session_token",
        new=AsyncMock(return_value=session_token),
    ), patch("app.core.security.touch_session_activity", new=AsyncMock()):
        result = await get_session_from_request(_request())

    assert result is None
    conn.execute.assert_awaited_once()
    assert "end_reason = 'customer_role_denied'" in conn.execute.await_args.args[0]
    assert conn.execute.await_args.args[1] == session_token
