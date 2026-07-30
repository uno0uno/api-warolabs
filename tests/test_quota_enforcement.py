from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.core.exceptions import APIError
from app.services import (
    api_tokens_service,
    billing_service,
    invitation_service,
    online_cart_service,
    public_restaurant_service,
    stations_service,
    tables_service,
)


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
    assert "tenant_invitations" in count_args[0]
    assert "customer" not in count_args[2]


@pytest.mark.asyncio
async def test_admin_users_quota_counts_pending_invitations():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "plan_slug": "pro",
        "plan_features": {"quotas": {"admin_users": 6}},
    })
    conn.fetchval = AsyncMock(return_value=6)

    with pytest.raises(APIError) as exc:
        await billing_service.check_plan_quota_growth(conn, tenant_id, "admin_users")

    assert exc.value.status_code == 429
    assert exc.value.details["resource"] == "admin_users"
    assert exc.value.details["used"] == 6
    count_args = conn.fetchval.await_args.args
    assert "tenant_invitations" in count_args[0]
    assert count_args[3] is None


@pytest.mark.asyncio
async def test_accept_invitation_excludes_current_pending_invitation_from_reserved_quota():
    tenant_id = uuid4()
    invitation_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "plan_slug": "pro",
        "plan_features": {"quotas": {"admin_users": 6}},
    })
    conn.fetchval = AsyncMock(return_value=5)

    await billing_service.check_plan_quota_growth(
        conn,
        tenant_id,
        "admin_users",
        exclude_pending_invitation_id=invitation_id,
    )

    count_args = conn.fetchval.await_args.args
    assert "ti.id = $3" in count_args[0]
    assert count_args[3] == invitation_id


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
    assert exc.value.details["plan_limit"] == 2
    assert exc.value.details["override"] is None
    assert exc.value.details["plan_slug"] == "pro"
    assert exc.value.details["upgrade_url"] == "/billing/planes"


@pytest.mark.asyncio
async def test_check_plan_quota_growth_uses_tenant_override_precedence():
    tenant_id = uuid4()
    override_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "plan_slug": "pro",
        "plan_features": {"quotas": {"active_kitchens": 2}},
        "override_id": override_id,
        "limit_override": 3,
        "override_disabled": False,
        "override_reason": "Commercial exception",
    })
    conn.fetchval = AsyncMock(return_value=2)

    await billing_service.check_plan_quota_growth(conn, tenant_id, "active_kitchens")

    conn.fetchval.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_plan_quota_growth_blocks_at_override_limit_with_metadata():
    tenant_id = uuid4()
    override_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "plan_slug": "pro",
        "plan_features": {"quotas": {"active_kitchens": 2}},
        "override_id": override_id,
        "limit_override": 3,
        "override_disabled": False,
        "override_reason": "Commercial exception",
    })
    conn.fetchval = AsyncMock(return_value=3)

    with pytest.raises(APIError) as exc:
        await billing_service.check_plan_quota_growth(conn, tenant_id, "active_kitchens")

    assert exc.value.details["limit"] == 3
    assert exc.value.details["plan_limit"] == 2
    assert exc.value.details["override"] == {
        "id": str(override_id),
        "disabled": False,
        "reason": "Commercial exception",
    }


@pytest.mark.asyncio
async def test_check_plan_quota_growth_disabled_override_is_unlimited():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "plan_slug": "pro",
        "plan_features": {"quotas": {"active_kitchens": 2}},
        "override_id": uuid4(),
        "limit_override": None,
        "override_disabled": True,
        "override_reason": "Unlimited pilot",
    })
    conn.fetchval = AsyncMock()

    await billing_service.check_plan_quota_growth(conn, tenant_id, "active_kitchens")

    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_plan_quota_growth_without_active_subscription_is_noop():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock()

    with patch.object(
        billing_service,
        "get_effective_plan_slug",
        new=AsyncMock(return_value=None),
    ):
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


@pytest.mark.asyncio
async def test_completed_online_order_quota_allows_below_limit_and_counts_completed_online_orders():
    tenant_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "plan_slug": "pro",
        "plan_features": {"quotas": {"completed_online_orders_per_month": 300}},
        "current_period_start": period_start,
        "current_period_end": period_end,
    })
    conn.fetchval = AsyncMock(return_value=299)

    await billing_service.check_completed_online_order_quota(conn, tenant_id)

    count_args = conn.fetchval.await_args.args
    assert "o.online_cart_id IS NOT NULL" in count_args[0]
    assert "o.status = 'completed'" in count_args[0]
    assert "o.order_date >= $2" in count_args[0]
    assert "o.order_date < $3" in count_args[0]
    assert count_args[1:] == (tenant_id, period_start, period_end)


@pytest.mark.asyncio
async def test_completed_online_order_quota_blocks_at_limit_with_stable_payload():
    tenant_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "plan_slug": "pro",
        "plan_features": {"quotas": {"completed_online_orders_per_month": 300}},
        "current_period_start": period_start,
        "current_period_end": period_end,
    })
    conn.fetchval = AsyncMock(return_value=300)

    with pytest.raises(APIError) as exc:
        await billing_service.check_completed_online_order_quota(conn, tenant_id)

    assert exc.value.status_code == 429
    assert exc.value.details["code"] == "online_order_quota_exceeded"
    assert exc.value.details["resource"] == "completed_online_orders_per_month"
    assert exc.value.details["used"] == 300
    assert exc.value.details["limit"] == 300
    assert exc.value.details["plan_limit"] == 300
    assert exc.value.details["override"] is None
    assert exc.value.details["plan_slug"] == "pro"
    assert exc.value.details["period_start"] == period_start.isoformat()
    assert exc.value.details["period_end"] == period_end.isoformat()
    assert exc.value.details["tenant_message"]
    assert exc.value.details["customer_message"]


@pytest.mark.asyncio
async def test_completed_online_order_disabled_override_skips_count():
    tenant_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "plan_slug": "pro",
        "plan_features": {"quotas": {"completed_online_orders_per_month": 300}},
        "current_period_start": period_start,
        "current_period_end": period_end,
        "override_id": uuid4(),
        "limit_override": None,
        "override_disabled": True,
        "override_reason": "Launch exception",
    })
    conn.fetchval = AsyncMock()

    await billing_service.check_completed_online_order_quota(conn, tenant_id)

    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_online_order_quota_without_active_subscription_is_noop():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock()

    with patch.object(
        billing_service,
        "get_effective_plan_slug",
        new=AsyncMock(return_value=None),
    ):
        await billing_service.check_completed_online_order_quota(conn, uuid4())

    conn.fetchval.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_online_order_quota_availability_hides_internal_payload_when_exhausted():
    tenant_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={
        "plan_slug": "pro",
        "plan_features": {"quotas": {"completed_online_orders_per_month": 300}},
        "current_period_start": period_start,
        "current_period_end": period_end,
    })
    conn.fetchval = AsyncMock(return_value=300)

    result = await billing_service.get_public_online_order_quota_availability(conn, tenant_id)

    assert result == {
        "available": False,
        "reason": "online_order_quota_exceeded",
        "message": billing_service.ONLINE_ORDER_QUOTA_CUSTOMER_MESSAGE,
    }
    assert "used" not in result
    assert "limit" not in result
    assert "plan_slug" not in result
    assert "override" not in result


@pytest.mark.asyncio
async def test_public_profile_exposes_safe_online_order_unavailable_signal_for_quota():
    tenant_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "slug": "restaurante",
            "is_active": True,
            "display_name": "Restaurante",
            "description": None,
            "logo_url": None,
            "banner_url": None,
            "phone_number": None,
            "email": None,
            "address": None,
            "city": None,
            "neighborhood": None,
            "latitude": None,
            "longitude": None,
            "timezone": "America/Bogota",
            "business_hours": {},
            "social_media": {},
            "seo_title": None,
            "seo_description": None,
            "accepts_online_orders": True,
            "min_order_amount": Decimal("0"),
            "estimated_preparation_time": 20,
            "is_manually_open": True,
            "tip_enabled": False,
            "tip_default_percentages": None,
            "tip_preselect_index": None,
            "created_at": None,
            "updated_at": None,
        },
        {
            "plan_slug": "pro",
            "plan_features": {"quotas": {"completed_online_orders_per_month": 300}},
            "current_period_start": period_start,
            "current_period_end": period_end,
        },
    ])
    conn.fetchval = AsyncMock(return_value=300)

    with (
        patch("app.services.public_restaurant_service.get_db_connection", side_effect=_db_context(conn)),
        patch("app.services.public_restaurant_service.is_currently_open", return_value=True),
    ):
        profile = await public_restaurant_service.get_profile_by_slug("restaurante")

    assert profile["is_currently_open"] is True
    assert profile["online_orders_available"] is False
    assert profile["online_orders_unavailable_reason"] == "online_order_quota_exceeded"
    assert profile["online_orders_unavailable_message"] == billing_service.ONLINE_ORDER_QUOTA_CUSTOMER_MESSAGE
    assert "used" not in profile
    assert "limit" not in profile
    assert "plan_slug" not in profile
    assert "override" not in profile


@pytest.mark.asyncio
async def test_checkout_online_order_quota_block_happens_before_cart_lock_and_order_insert():
    tenant_id = uuid4()
    cart_id = uuid4()
    customer_id = uuid4()
    product_id = uuid4()
    conn = MagicMock()
    conn.transaction.return_value = _tx()
    conn.fetch = AsyncMock(return_value=[])
    quota_error = APIError(
        "Límite mensual de pedidos en línea alcanzado",
        status_code=429,
        details={"code": "online_order_quota_exceeded"},
    )

    async def fetchrow(query, *args):
        if "FROM online_carts" in query:
            return {
                "id": cart_id,
                "tenant_id": tenant_id,
                "customer_id": customer_id,
                "order_type": "pickup",
                "delivery_address_id": None,
                "pickup_pin": "1234",
                "is_verified": True,
                "status": "active",
                "total_amount": Decimal("50000"),
                "verified_email": "cliente@example.com",
                "scheduled_time": None,
                "delivery_instructions": None,
            }
        if "FROM tenant_public_profiles" in query:
            return {
                "min_order_amount": Decimal("0"),
                "estimated_preparation_time": 20,
                "is_manually_open": True,
                "business_hours": {},
                "timezone": "America/Bogota",
                "accepts_online_orders": True,
                "tip_enabled": False,
            }
        if "UPDATE online_carts" in query or "INSERT INTO orders" in query:
            raise AssertionError("checkout side effect happened before quota block")
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow)

    with (
        patch("app.services.online_cart_service.get_db_connection", side_effect=_db_context(conn)),
        patch("app.services.online_cart_service.get_cart_items", new=AsyncMock(return_value=[{
            "product_id": str(product_id),
            "quantity": 1,
            "unit_price": Decimal("50000"),
            "subtotal": Decimal("50000"),
            "notes": None,
            "modifiers": [],
        }])),
        patch("app.services.public_restaurant_service.is_currently_open", return_value=True),
        patch("app.services.online_cart_service.check_completed_online_order_quota", new=AsyncMock(side_effect=quota_error)),
    ):
        with pytest.raises(APIError) as exc:
            await online_cart_service.checkout_cart(cart_id)

    assert exc.value.status_code == 429
    assert exc.value.details["code"] == "online_order_quota_exceeded"
    seen_queries = [call.args[0] for call in conn.fetchrow.await_args_list]
    assert not any("UPDATE online_carts SET status = 'checked_out'" in query for query in seen_queries)
    assert not any("INSERT INTO orders" in query for query in seen_queries)


def test_checkout_open_status_uses_tenant_timezone():
    import inspect

    source = inspect.getsource(online_cart_service.checkout_cart)
    assert "business_hours, timezone" in source
    assert "is_currently_open(bh, profile['is_manually_open'], profile['timezone'])" in source


@pytest.mark.asyncio
async def test_checkout_checked_out_cart_returns_existing_order_payload():
    tenant_id = uuid4()
    cart_id = uuid4()
    customer_id = uuid4()
    order_id = uuid4()
    conn = MagicMock()
    conn.transaction.return_value = _tx()

    async def fetchrow(query, *args):
        if "FROM online_carts" in query and "WHERE id = $1" in query:
            return {
                "id": cart_id,
                "tenant_id": tenant_id,
                "customer_id": customer_id,
                "order_type": "delivery",
                "delivery_address_id": uuid4(),
                "pickup_pin": None,
                "is_verified": True,
                "status": "checked_out",
                "total_amount": Decimal("50000"),
                "verified_email": "cliente@example.com",
                "scheduled_time": None,
                "delivery_instructions": None,
            }
        if "FROM orders o" in query and "o.online_cart_id = $1" in query:
            return {
                "id": order_id,
                "order_number": 12345,
                "total_amount": Decimal("50000"),
                "tip_amount": Decimal("3000"),
                "tip_source": "preset",
                "order_type": "delivery",
                "pickup_pin": None,
                "estimated_preparation_time": 35,
            }
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow)

    with (
        patch("app.services.online_cart_service.get_db_connection", side_effect=_db_context(conn)),
        patch("app.services.online_cart_service.get_cart_items", new=AsyncMock()) as get_cart_items,
        patch("app.services.online_cart_service.check_completed_online_order_quota", new=AsyncMock()) as quota_check,
    ):
        result = await online_cart_service.checkout_cart(cart_id)

    assert result["success"] is True
    assert result["data"]["order_id"] == str(order_id)
    assert result["data"]["order_number"] == 12345
    assert result["data"]["charged_amount"] == 53000.0
    get_cart_items.assert_not_awaited()
    quota_check.assert_not_awaited()


@pytest.mark.asyncio
async def test_checkout_checked_out_cart_without_order_stays_conflict():
    tenant_id = uuid4()
    cart_id = uuid4()
    conn = MagicMock()
    conn.transaction.return_value = _tx()

    async def fetchrow(query, *args):
        if "FROM online_carts" in query and "WHERE id = $1" in query:
            return {
                "id": cart_id,
                "tenant_id": tenant_id,
                "customer_id": uuid4(),
                "order_type": "delivery",
                "delivery_address_id": uuid4(),
                "pickup_pin": None,
                "is_verified": True,
                "status": "checked_out",
                "total_amount": Decimal("50000"),
                "verified_email": "cliente@example.com",
                "scheduled_time": None,
                "delivery_instructions": None,
            }
        if "FROM orders o" in query and "o.online_cart_id = $1" in query:
            return None
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow)

    with patch("app.services.online_cart_service.get_db_connection", side_effect=_db_context(conn)):
        with pytest.raises(HTTPException) as exc:
            await online_cart_service.checkout_cart(cart_id)

    assert exc.value.status_code == 409


def _starter_plan_row(resource: str, limit: int):
    return {
        "plan_slug": "starter",
        "plan_features": {"quotas": {resource: limit}},
        "override_id": None,
        "limit_override": None,
        "override_disabled": False,
        "override_reason": None,
    }


@pytest.mark.asyncio
async def test_starter_menu_products_quota_blocks_at_limit():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_starter_plan_row("menu_products", 10))
    conn.fetchval = AsyncMock(return_value=10)

    with pytest.raises(APIError) as exc:
        await billing_service.check_plan_quota_growth(conn, tenant_id, "menu_products")

    assert exc.value.status_code == 429
    assert exc.value.details["code"] == "quota_exceeded"
    assert exc.value.details["resource"] == "menu_products"


@pytest.mark.asyncio
async def test_starter_menu_categories_quota_allows_below_limit():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_starter_plan_row("menu_categories", 5))
    conn.fetchval = AsyncMock(return_value=4)

    await billing_service.check_plan_quota_growth(
        conn, tenant_id, "menu_categories"
    )


@pytest.mark.asyncio
async def test_starter_menu_categories_quota_blocks_at_limit():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_starter_plan_row("menu_categories", 5))
    conn.fetchval = AsyncMock(return_value=5)

    with pytest.raises(APIError) as exc:
        await billing_service.check_plan_quota_growth(
            conn, tenant_id, "menu_categories"
        )

    assert exc.value.status_code == 429
    assert exc.value.details["resource"] == "menu_categories"


@pytest.mark.asyncio
async def test_starter_tenant_ingredients_quota_blocks_at_limit():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_starter_plan_row("tenant_ingredients", 5))
    conn.fetchval = AsyncMock(return_value=5)

    with pytest.raises(APIError) as exc:
        await billing_service.check_plan_quota_growth(conn, tenant_id, "tenant_ingredients")

    assert exc.value.details["resource"] == "tenant_ingredients"


@pytest.mark.asyncio
async def test_scoped_recipe_lines_quota_blocks_over_limit():
    tenant_id = uuid4()
    product_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_starter_plan_row("recipe_lines_per_product", 4))
    conn.fetchval = AsyncMock(return_value=0)

    with pytest.raises(APIError) as exc:
        await billing_service.check_plan_quota_scoped(
            conn,
            tenant_id,
            "recipe_lines_per_product",
            product_id,
            projected_count=5,
        )

    assert exc.value.details["resource"] == "recipe_lines_per_product"
    assert exc.value.details["used"] == 5


@pytest.mark.asyncio
async def test_scoped_modifier_options_quota_blocks_over_limit():
    tenant_id = uuid4()
    group_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_starter_plan_row("modifier_options_per_group", 6))
    conn.fetchval = AsyncMock(return_value=0)

    with pytest.raises(APIError) as exc:
        await billing_service.check_plan_quota_scoped(
            conn,
            tenant_id,
            "modifier_options_per_group",
            group_id,
            projected_count=7,
        )

    assert exc.value.details["resource"] == "modifier_options_per_group"


@pytest.mark.asyncio
async def test_starter_online_order_quota_enforced_without_paid_subscription():
    tenant_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    conn = MagicMock()

    async def fetchrow(query, *args):
        if "FROM tenant_subscriptions ts" in query and "date_trunc('month'" not in query:
            return None
        if "FROM subscription_plans sp" in query and "date_trunc('month'" in query:
            return {
                "plan_slug": "starter",
                "plan_features": {"quotas": {"completed_online_orders_per_month": 30}},
                "current_period_start": period_start,
                "current_period_end": period_end,
                "override_id": None,
                "limit_override": None,
                "override_disabled": False,
                "override_reason": None,
            }
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetchval = AsyncMock(return_value=30)

    with patch.object(
        billing_service,
        "get_effective_plan_slug",
        new=AsyncMock(return_value="starter"),
    ):
        with pytest.raises(APIError) as exc:
            await billing_service.check_completed_online_order_quota(conn, tenant_id)

    assert exc.value.details["code"] == "online_order_quota_exceeded"


@pytest.mark.asyncio
async def test_assert_starter_toggle_rejects_tables_enabled():
    tenant_id = uuid4()
    conn = MagicMock()

    with patch.object(
        billing_service,
        "get_effective_plan_slug",
        new=AsyncMock(return_value="starter"),
    ):
        with pytest.raises(APIError) as exc:
            await billing_service.assert_starter_toggle_allowed(
                conn, tenant_id, "tables_enabled", True
            )

    assert exc.value.status_code == 403
    assert exc.value.details["code"] == "starter_plan_restriction"


@pytest.mark.asyncio
async def test_starter_modifier_groups_quota_blocks_at_limit():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_starter_plan_row("modifier_groups", 4))
    conn.fetchval = AsyncMock(return_value=4)

    with pytest.raises(APIError) as exc:
        await billing_service.check_plan_quota_growth(conn, tenant_id, "modifier_groups")

    assert exc.value.details["resource"] == "modifier_groups"


@pytest.mark.asyncio
async def test_starter_recipe_bases_quota_allows_below_limit():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_starter_plan_row("recipe_bases", 5))
    conn.fetchval = AsyncMock(return_value=4)

    await billing_service.check_plan_quota_growth(conn, tenant_id, "recipe_bases")


@pytest.mark.asyncio
async def test_starter_recipe_bases_quota_blocks_at_limit():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_starter_plan_row("recipe_bases", 5))
    conn.fetchval = AsyncMock(return_value=5)

    with pytest.raises(APIError) as exc:
        await billing_service.check_plan_quota_growth(conn, tenant_id, "recipe_bases")

    assert exc.value.status_code == 429
    assert exc.value.details["resource"] == "recipe_bases"


@pytest.mark.asyncio
async def test_starter_modifier_groups_quota_allows_below_limit():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_starter_plan_row("modifier_groups", 4))
    conn.fetchval = AsyncMock(return_value=3)

    await billing_service.check_plan_quota_growth(
        conn, tenant_id, "modifier_groups"
    )


@pytest.mark.asyncio
async def test_scoped_recipe_base_template_lines_quota_blocks_over_limit():
    tenant_id = uuid4()
    base_type_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_starter_plan_row("recipe_base_template_lines", 4))
    conn.fetchval = AsyncMock(return_value=0)

    with pytest.raises(APIError) as exc:
        await billing_service.check_plan_quota_scoped(
            conn,
            tenant_id,
            "recipe_base_template_lines",
            base_type_id,
            projected_count=5,
        )

    assert exc.value.details["resource"] == "recipe_base_template_lines"


@pytest.mark.asyncio
@pytest.mark.parametrize("toggle", ["comandas_enabled", "kds_enabled"])
async def test_assert_starter_toggle_rejects_locked_operaciones_toggles(toggle):
    tenant_id = uuid4()
    conn = MagicMock()

    with patch.object(
        billing_service,
        "get_effective_plan_slug",
        new=AsyncMock(return_value="starter"),
    ):
        with pytest.raises(APIError) as exc:
            await billing_service.assert_starter_toggle_allowed(conn, tenant_id, toggle, True)

    assert exc.value.details["code"] == "starter_plan_restriction"


@pytest.mark.asyncio
async def test_assert_starter_shift_template_growth_rejects_starter():
    tenant_id = uuid4()
    conn = MagicMock()

    with patch.object(
        billing_service,
        "get_effective_plan_slug",
        new=AsyncMock(return_value="starter"),
    ):
        with pytest.raises(APIError) as exc:
            await billing_service.assert_starter_shift_template_growth_allowed(
                conn, tenant_id
            )

    assert exc.value.status_code == 403
    assert exc.value.details["code"] == "starter_plan_restriction"
    assert exc.value.details["feature"] == "shift_templates"


@pytest.mark.asyncio
async def test_assert_starter_shift_template_growth_allows_non_starter():
    tenant_id = uuid4()
    conn = MagicMock()

    with patch.object(
        billing_service,
        "get_effective_plan_slug",
        new=AsyncMock(return_value="pro"),
    ):
        await billing_service.assert_starter_shift_template_growth_allowed(
            conn, tenant_id
        )


@pytest.mark.asyncio
async def test_default_scan_limit_for_starter_is_ten():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"scan_limit": 10})

    with patch.object(
        billing_service,
        "get_effective_plan_slug",
        new=AsyncMock(return_value="starter"),
    ):
        limit = await billing_service._default_scan_limit_for_tenant(conn, uuid4())

    assert limit == 10


@pytest.mark.asyncio
async def test_require_module_allows_finanzas_for_starter_plan():
    from app.core.permissions import Module, require_module

    session = SimpleNamespace(
        is_valid=True,
        tenant_id=uuid4(),
        user_id=uuid4(),
        role="owner",
    )
    dep = require_module(Module.FINANZAS)
    request = MagicMock()
    request.url.path = "/financiero/tir-metrics"

    with patch("app.core.middleware.get_session_context", return_value=session), \
         patch(
             "app.services.billing_service.get_effective_plan_slug",
             new=AsyncMock(return_value="starter"),
         ), \
         patch(
             "app.core.permissions.get_enforcement_mode",
             new=AsyncMock(return_value="disabled"),
         ), \
         patch("app.core.permissions.get_db_connection") as db_ctx:
        db_ctx.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        db_ctx.return_value.__aexit__ = AsyncMock(return_value=False)
        await dep(request)



def _period_plan_row(resource: str, limit: int, *, period_start=None, period_end=None):
    return {
        "plan_slug": "starter",
        "plan_features": {"quotas": {resource: limit}},
        "current_period_start": period_start or datetime(2026, 7, 1, tzinfo=timezone.utc),
        "current_period_end": period_end or datetime(2026, 8, 1, tzinfo=timezone.utc),
        "override_id": None,
        "limit_override": None,
        "override_disabled": False,
        "override_reason": None,
    }


@pytest.mark.asyncio
async def test_starter_tenant_suppliers_quota_blocks_at_limit():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_starter_plan_row("tenant_suppliers", 3))
    conn.fetchval = AsyncMock(return_value=3)

    with pytest.raises(APIError) as exc:
        await billing_service.check_plan_quota_growth(conn, tenant_id, "tenant_suppliers")

    assert exc.value.status_code == 429
    assert exc.value.details["code"] == "quota_exceeded"
    assert exc.value.details["resource"] == "tenant_suppliers"
    assert exc.value.details["used"] == 3
    assert exc.value.details["limit"] == 3
    count_sql = conn.fetchval.await_args.args[0]
    assert "FROM tenant_suppliers" in count_sql
    assert "is_active = TRUE" in count_sql


@pytest.mark.asyncio
async def test_starter_tenant_suppliers_quota_allows_below_limit():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_starter_plan_row("tenant_suppliers", 3))
    conn.fetchval = AsyncMock(return_value=2)

    await billing_service.check_plan_quota_growth(conn, tenant_id, "tenant_suppliers")


@pytest.mark.asyncio
async def test_direct_purchases_period_quota_blocks_at_limit():
    tenant_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value=_period_plan_row(
            "direct_purchases_per_period", 15, period_start=period_start, period_end=period_end
        )
    )
    conn.fetchval = AsyncMock(return_value=15)

    with pytest.raises(APIError) as exc:
        await billing_service.check_plan_quota_period(
            conn, tenant_id, "direct_purchases_per_period"
        )

    assert exc.value.status_code == 429
    assert exc.value.details["code"] == "quota_exceeded"
    assert exc.value.details["resource"] == "direct_purchases_per_period"
    assert exc.value.details["used"] == 15
    assert exc.value.details["limit"] == 15
    assert exc.value.details["period_start"] == period_start.isoformat()
    assert exc.value.details["period_end"] == period_end.isoformat()
    count_args = conn.fetchval.await_args.args
    assert "FROM tenant_purchases" in count_args[0]
    assert "is_direct_entry = TRUE" in count_args[0]
    assert "created_at >= $2" in count_args[0]
    assert count_args[1:] == (tenant_id, period_start, period_end)


@pytest.mark.asyncio
async def test_direct_purchases_period_quota_allows_below_limit():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_period_plan_row("direct_purchases_per_period", 15))
    conn.fetchval = AsyncMock(return_value=14)

    await billing_service.check_plan_quota_period(
        conn, tenant_id, "direct_purchases_per_period"
    )


@pytest.mark.asyncio
async def test_stock_adjustments_period_quota_blocks_at_limit():
    tenant_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value=_period_plan_row(
            "stock_adjustments_per_period", 20, period_start=period_start, period_end=period_end
        )
    )
    conn.fetchval = AsyncMock(return_value=20)

    with pytest.raises(APIError) as exc:
        await billing_service.check_plan_quota_period(
            conn, tenant_id, "stock_adjustments_per_period"
        )

    assert exc.value.status_code == 429
    assert exc.value.details["code"] == "quota_exceeded"
    assert exc.value.details["resource"] == "stock_adjustments_per_period"
    assert exc.value.details["used"] == 20
    assert exc.value.details["limit"] == 20
    count_args = conn.fetchval.await_args.args
    assert "FROM tenant_ingredient_movements" in count_args[0]
    assert "movement_type = 'adjustment'" in count_args[0]
    assert "COALESCE(reference_table, '') <> 'tenant_purchases'" in count_args[0]
    assert count_args[1:] == (tenant_id, period_start, period_end)


@pytest.mark.asyncio
async def test_stock_adjustments_period_quota_allows_below_limit():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_period_plan_row("stock_adjustments_per_period", 20))
    conn.fetchval = AsyncMock(return_value=19)

    await billing_service.check_plan_quota_period(
        conn, tenant_id, "stock_adjustments_per_period"
    )


@pytest.mark.asyncio
async def test_cash_closes_period_quota_blocks_at_limit():
    tenant_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value=_period_plan_row(
            "cash_closes_per_period", 30, period_start=period_start, period_end=period_end
        )
    )
    conn.fetchval = AsyncMock(return_value=30)

    with pytest.raises(APIError) as exc:
        await billing_service.check_plan_quota_period(
            conn, tenant_id, "cash_closes_per_period"
        )

    assert exc.value.status_code == 429
    assert exc.value.details["resource"] == "cash_closes_per_period"
    assert "FROM accounting_period" in conn.fetchval.await_args.args[0]


@pytest.mark.asyncio
async def test_manual_journal_entries_period_quota_filters_source_module():
    tenant_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value=_period_plan_row(
            "manual_journal_entries_per_period",
            30,
            period_start=period_start,
            period_end=period_end,
        )
    )
    conn.fetchval = AsyncMock(return_value=30)

    with pytest.raises(APIError) as exc:
        await billing_service.check_plan_quota_period(
            conn, tenant_id, "manual_journal_entries_per_period"
        )

    assert exc.value.status_code == 429
    sql = conn.fetchval.await_args.args[0]
    assert "FROM tenant_journal_entries" in sql
    assert "manual_balance_adjustment" in sql
    assert "source_module IN" in sql


@pytest.mark.asyncio
async def test_expenses_period_quota_counts_expenses_and_instances():
    tenant_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value=_period_plan_row(
            "expenses_per_period", 30, period_start=period_start, period_end=period_end
        )
    )
    conn.fetchval = AsyncMock(return_value=30)

    with pytest.raises(APIError):
        await billing_service.check_plan_quota_period(
            conn, tenant_id, "expenses_per_period"
        )

    sql = conn.fetchval.await_args.args[0]
    assert "FROM tenant_expenses" in sql
    assert "FROM recurring_expense_instances" in sql


@pytest.mark.asyncio
async def test_active_open_cash_shifts_growth_quota_blocks_at_limit():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_starter_plan_row("active_open_cash_shifts", 1))
    conn.fetchval = AsyncMock(return_value=1)

    with pytest.raises(APIError) as exc:
        await billing_service.check_plan_quota_growth(
            conn, tenant_id, "active_open_cash_shifts"
        )

    assert exc.value.status_code == 429
    assert exc.value.details["resource"] == "active_open_cash_shifts"
    assert "FROM cash_shift_openings" in conn.fetchval.await_args.args[0]
    assert "status = 'open'" in conn.fetchval.await_args.args[0]


@pytest.mark.asyncio
async def test_payment_methods_growth_quota_allows_below_limit():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_starter_plan_row("payment_methods", 5))
    conn.fetchval = AsyncMock(return_value=4)

    await billing_service.check_plan_quota_growth(conn, tenant_id, "payment_methods")


@pytest.mark.asyncio
async def test_api_tokens_growth_quota_blocks_at_starter_zero():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_starter_plan_row("api_tokens", 0))
    conn.fetchval = AsyncMock(return_value=0)

    with pytest.raises(APIError) as exc:
        await billing_service.check_plan_quota_growth(conn, tenant_id, "api_tokens")

    assert exc.value.status_code == 429
    assert exc.value.details["code"] == "quota_exceeded"
    assert exc.value.details["resource"] == "api_tokens"
    assert exc.value.details["limit"] == 0
    assert "FROM api_tokens" in conn.fetchval.await_args.args[0]
    assert "is_active = TRUE" in conn.fetchval.await_args.args[0]


@pytest.mark.asyncio
async def test_tenant_promotions_growth_quota_blocks_at_starter_one():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_starter_plan_row("tenant_promotions", 1))
    conn.fetchval = AsyncMock(return_value=1)

    with pytest.raises(APIError) as exc:
        await billing_service.check_plan_quota_growth(
            conn, tenant_id, "tenant_promotions"
        )

    assert exc.value.status_code == 429
    assert exc.value.details["code"] == "quota_exceeded"
    assert exc.value.details["resource"] == "tenant_promotions"
    assert exc.value.details["limit"] == 1
    assert "FROM tenant_promotions" in conn.fetchval.await_args.args[0]


@pytest.mark.asyncio
async def test_tenant_promotions_growth_quota_allows_below_starter_limit():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=_starter_plan_row("tenant_promotions", 1))
    conn.fetchval = AsyncMock(return_value=0)

    await billing_service.check_plan_quota_growth(conn, tenant_id, "tenant_promotions")


@pytest.mark.asyncio
async def test_tenant_promotions_growth_quota_allows_unlimited_pro():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "plan_slug": "pro",
            "plan_features": {
                "quotas": {"tenant_promotions": billing_service.CATALOG_UNLIMITED}
            },
            "override_limit": None,
            "override_expires_at": None,
        }
    )
    conn.fetchval = AsyncMock(return_value=50)

    await billing_service.check_plan_quota_growth(conn, tenant_id, "tenant_promotions")


@pytest.mark.asyncio
async def test_api_tokens_growth_quota_allows_below_paid_limit():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "plan_slug": "pro",
            "plan_features": {"quotas": {"api_tokens": billing_service.CATALOG_UNLIMITED}},
            "override_id": None,
            "limit_override": None,
            "override_disabled": False,
            "override_reason": None,
        }
    )
    conn.fetchval = AsyncMock(return_value=2)

    await billing_service.check_plan_quota_growth(conn, tenant_id, "api_tokens")


@pytest.mark.asyncio
async def test_api_token_reactivate_checks_growth_quota():
    tenant_id = uuid4()
    user_id = uuid4()
    token_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"role": "admin"},
            {"is_active": False},
        ]
    )
    quota_error = APIError("Límite del plan alcanzado", status_code=429)
    session = {"user_id": str(user_id), "tenant_id": str(tenant_id)}

    with (
        patch(
            "app.services.api_tokens_service.get_session_from_request",
            new=AsyncMock(return_value=session),
        ),
        patch(
            "app.services.api_tokens_service.get_db_connection",
            side_effect=_db_context(conn),
        ),
        patch(
            "app.services.api_tokens_service.check_plan_quota_growth",
            new=AsyncMock(side_effect=quota_error),
        ) as quota_check,
    ):
        with pytest.raises(APIError) as exc:
            await api_tokens_service.update_api_token(
                _request(),
                str(token_id),
                is_active=True,
            )

    assert exc.value.status_code == 429
    quota_check.assert_awaited_once()
    assert quota_check.await_args.args[2] == "api_tokens"
    assert conn.fetchrow.await_count == 2


@pytest.mark.asyncio
async def test_supplier_payments_period_quota_uses_paid_at():
    tenant_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value=_period_plan_row(
            "supplier_payments_per_period",
            30,
            period_start=period_start,
            period_end=period_end,
        )
    )
    conn.fetchval = AsyncMock(return_value=29)

    await billing_service.check_plan_quota_period(
        conn, tenant_id, "supplier_payments_per_period"
    )
    sql = conn.fetchval.await_args.args[0]
    assert "paid_at" in sql
    assert "FROM tenant_purchases" in sql


@pytest.mark.asyncio
async def test_accounting_period_closes_period_quota_blocks_at_limit():
    tenant_id = uuid4()
    period_start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    period_end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value=_period_plan_row(
            "accounting_period_closes_per_period",
            3,
            period_start=period_start,
            period_end=period_end,
        )
    )
    conn.fetchval = AsyncMock(return_value=3)

    with pytest.raises(APIError) as exc:
        await billing_service.check_plan_quota_period(
            conn, tenant_id, "accounting_period_closes_per_period"
        )

    assert exc.value.details["resource"] == "accounting_period_closes_per_period"
    assert "FROM tenant_monthly_periods" in conn.fetchval.await_args.args[0]


def test_manual_journal_allowed_source_modules_match_count_filter():
    """Public JE create whitelist must stay aligned with period count SQL."""
    from app.services.accounting_service import MANUAL_JOURNAL_SOURCE_MODULES

    assert MANUAL_JOURNAL_SOURCE_MODULES == frozenset(
        {"manual", "manual_balance_adjustment"}
    )
    assert "system" not in MANUAL_JOURNAL_SOURCE_MODULES
    assert "orden" not in MANUAL_JOURNAL_SOURCE_MODULES


# ── CRM unlimited-by-design (#1932) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_crm_waros_rules_quota_resource_is_rejected():
    """Waros rule growth must not be enforceable via plan quota helpers."""
    conn = MagicMock()
    with pytest.raises(ValueError, match="Unsupported quota resource"):
        await billing_service.check_plan_quota_growth(conn, uuid4(), "waros_rules")


@pytest.mark.asyncio
async def test_crm_wallet_recharge_period_quota_resource_is_rejected():
    conn = MagicMock()
    with pytest.raises(ValueError, match="Unsupported period quota resource"):
        await billing_service.check_plan_quota_period(conn, uuid4(), "wallet_recharges")


@pytest.mark.asyncio
async def test_crm_cartera_credit_payment_period_quota_resource_is_rejected():
    conn = MagicMock()
    with pytest.raises(ValueError, match="Unsupported period quota resource"):
        await billing_service.check_plan_quota_period(conn, uuid4(), "cartera_payments")


def test_crm_service_modules_do_not_call_plan_quota_helpers():
    """Wallet / credit / waros services must stay off billing quota enforcement."""
    import inspect

    from app.services import credit_service, customer_wallet_service, waros_service

    for mod in (customer_wallet_service, credit_service, waros_service):
        src = inspect.getsource(mod)
        assert "check_plan_quota_growth" not in src, mod.__name__
        assert "check_plan_quota_period" not in src, mod.__name__
        assert "check_plan_quota_scoped" not in src, mod.__name__
        assert "ENFORCEABLE_QUOTA_RESOURCES" not in src, mod.__name__
