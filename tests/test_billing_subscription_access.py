"""Grace-period access levels via get_subscription_access (#62, #363)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services import billing_service


def _conn_with_subscription(
    status: str,
    period_end: datetime | None,
    *,
    trial_ends_at: datetime | None = None,
):
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "status": status,
            "current_period_end": period_end,
            "plan_id": uuid4(),
            "trial_started_at": (
                trial_ends_at - timedelta(days=15) if trial_ends_at else None
            ),
            "trial_ends_at": trial_ends_at,
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
async def test_no_subscription_returns_free():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)

    access = await billing_service.get_subscription_access(tenant_id, conn)

    assert access.level == "free"
    conn.fetchrow.assert_called_once()


@pytest.mark.asyncio
async def test_trialing_before_exact_boundary_returns_full_without_paid_grace():
    frozen = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    trial_end = frozen + timedelta(seconds=1)
    conn = _conn_with_subscription("trialing", trial_end, trial_ends_at=trial_end)

    with patch.object(billing_service, "datetime") as mocked_datetime:
        mocked_datetime.now.return_value = frozen
        access = await billing_service.get_subscription_access(uuid4(), conn)

    assert access.level == "full"
    assert access.subscription_status == "trialing"
    assert access.grace_days_remaining == 0
    assert access.trial_days_remaining == 1
    assert access.trial_ends_at == trial_end.isoformat()


@pytest.mark.asyncio
async def test_trialing_at_exact_boundary_is_effectively_expired_and_read_only():
    frozen = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    conn = _conn_with_subscription("trialing", frozen, trial_ends_at=frozen)

    with patch.object(billing_service, "datetime") as mocked_datetime:
        mocked_datetime.now.return_value = frozen
        access = await billing_service.get_subscription_access(uuid4(), conn)

    assert access.level == "read_only"
    assert access.subscription_status == "trial_expired"
    assert access.grace_days_remaining == 0
    assert access.trial_days_remaining == 0


@pytest.mark.asyncio
async def test_persisted_trial_expired_never_inherits_paid_grace():
    trial_end = datetime.now(timezone.utc) - timedelta(days=1)
    conn = _conn_with_subscription("trial_expired", trial_end, trial_ends_at=trial_end)

    access = await billing_service.get_subscription_access(uuid4(), conn)

    assert access.level == "read_only"
    assert access.subscription_status == "trial_expired"
    assert access.grace_days_remaining == 0
