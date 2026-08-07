"""Grace-period access levels via get_subscription_access (#62, #363)."""
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import APIError
from app.services import billing_service


def _conn_with_subscription(status: str, period_end: Optional[datetime]):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "status": status,
            "current_period_end": period_end,
            "plan_id": uuid4(),
        }
    )
    return conn


@pytest.mark.asyncio
async def test_past_due_early_grace_returns_full_with_warning():
    tenant_id = uuid4()
    period_end = datetime.now(timezone.utc) - timedelta(days=2)
    conn = _conn_with_subscription("past_due", period_end)

    access = await billing_service.get_subscription_access(tenant_id, conn)

    assert access.level == "full_with_warning"
    assert access.subscription_status == "past_due"
    assert access.grace_days_remaining == 5


@pytest.mark.asyncio
async def test_past_due_mid_grace_returns_read_only():
    tenant_id = uuid4()
    period_end = datetime.now(timezone.utc) - timedelta(days=5)
    conn = _conn_with_subscription("past_due", period_end)

    access = await billing_service.get_subscription_access(tenant_id, conn)

    assert access.level == "read_only"
    assert access.grace_days_remaining == 2


@pytest.mark.asyncio
async def test_past_due_after_grace_window_returns_blocked():
    tenant_id = uuid4()
    period_end = datetime.now(timezone.utc) - timedelta(days=10)
    conn = _conn_with_subscription("past_due", period_end)

    access = await billing_service.get_subscription_access(tenant_id, conn)

    assert access.level == "blocked"
    assert access.grace_days_remaining == 0


@pytest.mark.asyncio
async def test_active_subscription_returns_full():
    tenant_id = uuid4()
    period_end = datetime.now(timezone.utc) + timedelta(days=20)
    conn = _conn_with_subscription("active", period_end)

    access = await billing_service.get_subscription_access(tenant_id, conn)

    assert access.level == "full"
    assert access.subscription_status == "active"


@pytest.mark.asyncio
async def test_pending_checkout_returns_blocked():
    tenant_id = uuid4()
    period_end = datetime.now(timezone.utc) + timedelta(days=20)
    conn = _conn_with_subscription("pending", period_end)

    access = await billing_service.get_subscription_access(tenant_id, conn)

    assert access.level == "blocked"
    assert access.subscription_status == "pending"
    assert access.grace_days_remaining == 0
    assert access.next_payment_date is None
    assert "Completa el pago pendiente" in access.message


@pytest.mark.asyncio
async def test_cancelled_subscription_returns_blocked():
    tenant_id = uuid4()
    period_end = datetime.now(timezone.utc) + timedelta(days=20)
    conn = _conn_with_subscription("cancelled", period_end)

    access = await billing_service.get_subscription_access(tenant_id, conn)

    assert access.level == "blocked"
    assert access.subscription_status == "cancelled"


@pytest.mark.asyncio
async def test_expired_subscription_returns_blocked():
    tenant_id = uuid4()
    period_end = datetime.now(timezone.utc) - timedelta(days=1)
    conn = _conn_with_subscription("expired", period_end)

    access = await billing_service.get_subscription_access(tenant_id, conn)

    assert access.level == "blocked"
    assert access.subscription_status == "expired"


@pytest.mark.asyncio
async def test_no_subscription_returns_starter():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=None)

    access = await billing_service.get_subscription_access(tenant_id, conn)

    assert access.level == "starter"
    conn.fetchrow.assert_called_once()


@pytest.mark.asyncio
async def test_payment_pending_onboarding_without_subscription_is_blocked():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value="payment_pending")

    access = await billing_service.get_subscription_access(tenant_id, conn)

    assert access.level == "blocked"
    assert access.subscription_status == "payment_pending"
    assert "Mi Plan" in access.message
    assert "completa el pago" in access.message


@pytest.mark.asyncio
async def test_get_effective_plan_slug_returns_starter_without_paid_sub():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[None, None])
    conn.fetchval = AsyncMock(return_value="starter_active")

    slug = await billing_service.get_effective_plan_slug(conn, tenant_id)

    assert slug == billing_service.STARTER_PLAN_SLUG


@pytest.mark.asyncio
async def test_check_plan_quota_growth_blocks_starter_table_growth_at_zero():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[
        None,
        None,
        {
            "plan_slug": billing_service.STARTER_PLAN_SLUG,
            "plan_features": {"quotas": billing_service.STARTER_OPERATIONAL_QUOTAS},
            "override_id": None,
            "limit_override": None,
            "override_disabled": False,
            "override_reason": None,
        },
    ])
    conn.fetchval = AsyncMock(return_value="starter_active")

    with patch.object(
        billing_service,
        "_count_quota_resource_usage",
        new=AsyncMock(return_value=0),
    ):
        with pytest.raises(APIError) as exc:
            await billing_service.check_plan_quota_growth(
                conn,
                tenant_id,
                "active_tables_including_bar",
            )

    assert exc.value.details["code"] == "quota_exceeded"
    assert exc.value.details["plan_slug"] == billing_service.STARTER_PLAN_SLUG
