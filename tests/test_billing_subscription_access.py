"""Grace-period access levels via get_subscription_access (#62, #363)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.services import billing_service


def _conn_with_subscription(status: str, period_end: datetime | None):
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
async def test_no_subscription_returns_free():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)

    access = await billing_service.get_subscription_access(tenant_id, conn)

    assert access.level == "free"
    conn.fetchrow.assert_called_once()
