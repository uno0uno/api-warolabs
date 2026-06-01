"""Failed payment handling — grace only when period overdue (#365)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.services import billing_service


def _subscription_row(*, status: str, period_end: datetime):
    return {
        "subscription_id": uuid4(),
        "tenant_id": uuid4(),
        "status": status,
        "current_period_end": period_end,
    }


@pytest.mark.asyncio
async def test_declined_with_valid_period_does_not_mark_past_due():
    row = _subscription_row(
        status="active",
        period_end=datetime.now(timezone.utc) + timedelta(days=20),
    )
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[
        row,
        {"name": "Test", "email": "t@example.com"},
    ])
    conn.execute = AsyncMock()

    result = await billing_service.mark_subscription_past_due(
        conn, "SD7wnV", "payment_rejected",
    )

    assert result is not None
    update_calls = [
        c for c in conn.execute.call_args_list
        if "UPDATE tenant_subscriptions" in c[0][0]
    ]
    assert len(update_calls) == 0
    event_sql = conn.execute.call_args_list[0][0][0]
    assert "billing_events" in event_sql
    assert conn.execute.call_args_list[0][0][3] == "payment_rejected"


@pytest.mark.asyncio
async def test_declined_pending_checkout_does_not_mark_past_due():
    row = _subscription_row(
        status="pending",
        period_end=datetime.now(timezone.utc) + timedelta(days=30),
    )
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[
        row,
        {"name": "Test", "email": "t@example.com"},
    ])
    conn.execute = AsyncMock()

    await billing_service.mark_subscription_past_due(
        conn, "LINK1", "payment_rejected",
    )

    update_calls = [
        c for c in conn.execute.call_args_list
        if "UPDATE tenant_subscriptions" in c[0][0]
    ]
    assert len(update_calls) == 0


@pytest.mark.asyncio
async def test_declined_with_expired_period_marks_past_due():
    row = _subscription_row(
        status="active",
        period_end=datetime.now(timezone.utc) - timedelta(days=3),
    )
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[
        row,
        {"name": "Test", "email": "t@example.com"},
    ])
    conn.execute = AsyncMock()

    await billing_service.mark_subscription_past_due(
        conn, "SD7wnV", "payment_rejected",
    )

    update_calls = [
        c for c in conn.execute.call_args_list
        if "UPDATE tenant_subscriptions" in c[0][0]
    ]
    assert len(update_calls) == 1
    assert "past_due" in update_calls[0][0][0]


@pytest.mark.asyncio
async def test_declined_when_already_past_due_stays_past_due():
    row = _subscription_row(
        status="past_due",
        period_end=datetime.now(timezone.utc) - timedelta(days=10),
    )
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=[
        row,
        {"name": "Test", "email": "t@example.com"},
    ])
    conn.execute = AsyncMock()

    await billing_service.mark_subscription_past_due(
        conn, "SD7wnV", "payment_rejected",
    )

    update_calls = [
        c for c in conn.execute.call_args_list
        if "UPDATE tenant_subscriptions" in c[0][0]
    ]
    assert len(update_calls) == 1
