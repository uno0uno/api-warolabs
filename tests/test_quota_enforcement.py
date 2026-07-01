from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import APIError
from app.services import billing_service, invitation_service, stations_service, tables_service


def _db_context(conn):
    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx


def _tx():
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    return tx


def _session(tenant_id=None):
    return SimpleNamespace(user_id=uuid4(), tenant_id=tenant_id or uuid4())


def _request():
    request = MagicMock()
    request.headers = {"user-agent": "pytest"}
    return request


@pytest.mark.asyncio
async def test_check_plan_quota_growth_allows_below_limit_and_excludes_customers():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "plan_slug": "pro",
        "plan_features": {"quotas": {"admin_users": 2}},
    })
    conn.fetchval = AsyncMock(return_value=1)

    await billing_service.check_plan_quota_growth(conn, tenant_id, "admin_users")

    count_args = conn.fetchval.await_args.args
    assert "role = ANY" in count_args[0]
    assert "customer" not in count_args[2]


@pytest.mark.asyncio
async def test_check_plan_quota_growth_blocks_at_limit_with_stable_payload():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "plan_slug": "pro",
        "plan_features": {"quotas": {"active_kitchens": 2}},
    })
    conn.fetchval = AsyncMock(return_value=2)

    with pytest.raises(APIError) as exc:
        await billing_service.check_plan_quota_growth(conn, tenant_id, "active_kitchens")

    assert exc.value.status_code == 429
    assert exc.value.details["code"] == "quota_exceeded"
    assert exc.value.details["resource"] == "active_kitchens"
    assert exc.value.details["used"] == 2
    assert exc.value.details["limit"] == 2
    assert exc.value.details["plan_slug"] == "pro"
    assert exc.value.details["upgrade_url"] == "/billing/planes"


@pytest.mark.asyncio
async def test_check_plan_quota_growth_without_active_subscription_is_noop():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock()

    await billing_service.check_plan_quota_growth(conn, uuid4(), "active_tables_including_bar")

    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_table_quota_count_includes_bar_by_not_filtering_is_bar():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "plan_slug": "pro",
        "plan_features": {"quotas": {"active_tables_including_bar": 20}},
    })
    conn.fetchval = AsyncMock(return_value=19)

    await billing_service.check_plan_quota_growth(conn, tenant_id, "active_tables_including_bar")

    query = conn.fetchval.await_args.args[0]
    assert "deleted_at IS NULL" in query
    assert "is_bar" not in query


@pytest.mark.asyncio
async def test_accept_invitation_quota_block_does_not_accept_or_create_member():
    tenant_id = uuid4()
    profile_id = uuid4()
    invitation_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {
            "id": invitation_id,
            "tenant_id": tenant_id,
            "user_id": profile_id,
            "email": "new-admin@example.com",
            "name": "New Admin",
            "role": "admin",
        },
        None,
    ])
    conn.execute = AsyncMock()
    quota_error = APIError("Límite del plan alcanzado", status_code=429)

    with (
        patch("app.services.invitation_service.require_valid_tenant", return_value=SimpleNamespace(site="tenant.example.com")),
        patch("app.services.invitation_service.get_db_connection", side_effect=_db_context(conn)),
        patch("app.services.invitation_service.check_plan_quota_growth", new=AsyncMock(side_effect=quota_error)),
    ):
        with pytest.raises(APIError):
            await invitation_service.accept_invitation(_request(), MagicMock(), "token")

    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_station_create_checks_quota_before_insert():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock()
    quota_error = APIError("Límite del plan alcanzado", status_code=429)
    body = SimpleNamespace(
        name="Parrilla",
        kitchen_name="Cocina",
        color="#ff0000",
        alert_threshold_1_min=5,
        alert_threshold_2_min=10,
        display_order=1,
    )

    with (
        patch("app.services.stations_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.stations_service.get_db_connection", side_effect=_db_context(conn)),
        patch("app.services.stations_service.check_plan_quota_growth", new=AsyncMock(side_effect=quota_error)),
    ):
        with pytest.raises(APIError):
            await stations_service.create_station(_request(), body)

    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_station_deactivation_does_not_check_growth_quota():
    tenant_id = uuid4()
    station_id = uuid4()
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=0)
    conn.fetchrow = AsyncMock(return_value={"id": station_id, "is_active": False})

    with (
        patch("app.services.stations_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.stations_service.get_db_connection", side_effect=_db_context(conn)),
        patch("app.services.stations_service.check_plan_quota_growth", new=AsyncMock()) as quota_check,
    ):
        await stations_service.toggle_station(_request(), station_id, False)

    quota_check.assert_not_awaited()


@pytest.mark.asyncio
async def test_table_create_checks_quota_before_insert():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.transaction.return_value = _tx()
    conn.fetchrow = AsyncMock()
    quota_error = APIError("Límite del plan alcanzado", status_code=429)

    with (
        patch("app.services.tables_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(conn)),
        patch("app.services.tables_service._resolve_table_code", new=AsyncMock(return_value="M1")),
        patch("app.services.tables_service.check_plan_quota_growth", new=AsyncMock(side_effect=quota_error)),
    ):
        with pytest.raises(APIError):
            await tables_service.create_table(_request(), "Mesa 1", 4)

    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_table_qr_disable_does_not_check_growth_quota():
    tenant_id = uuid4()
    table_id = uuid4()
    conn = MagicMock()
    conn.transaction.return_value = _tx()
    row = {
        "id": table_id,
        "name": "Mesa 2",
        "capacity": 4,
        "status": "free",
        "is_active": True,
        "is_bar": False,
        "qr_enabled": False,
        "qr_public_token": "tok",
        "created_at": MagicMock(isoformat=lambda: "2026-01-01T00:00:00"),
    }
    conn.fetchval = AsyncMock(return_value=True)
    conn.fetchrow = AsyncMock(side_effect=[
        {
            "id": table_id,
            "name": "Mesa 2",
            "is_bar": False,
            "is_active": True,
            "deleted_at": None,
            "qr_enabled": True,
            "qr_public_token": "tok",
        },
        row,
    ])

    with (
        patch("app.services.tables_service.require_valid_session", return_value=_session(tenant_id)),
        patch("app.services.tables_service.get_db_connection", side_effect=_db_context(conn)),
        patch("app.services.tables_service.check_plan_quota_growth", new=AsyncMock()) as quota_check,
    ):
        await tables_service.set_table_qr_enabled(_request(), table_id, False)

    quota_check.assert_not_awaited()
