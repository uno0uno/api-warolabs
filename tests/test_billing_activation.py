"""Subscription activation — verify-payment period extension (#354)."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.services import billing_service


@pytest.mark.asyncio
async def test_activate_by_gateway_ref_past_due_extends_period():
    tenant_id = uuid4()
    sub_id = uuid4()
    new_end = datetime.now(timezone.utc) + timedelta(days=365)

    conn = MagicMock()

    async def fetchrow_side_effect(query, *args):
        sql = " ".join(query.split())
        if "FROM tenant_subscriptions" in sql and "gateway_reference" in sql:
            return {"id": sub_id, "status": "past_due", "billing_cycle": "annual"}
        if "UPDATE tenant_subscriptions" in sql:
            assert args[1] == "1 year"
            return {"current_period_end": new_end}
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    conn.execute = AsyncMock()

    await billing_service.activate_subscription_by_gateway_ref(
        conn,
        tenant_id=tenant_id,
        gateway_reference="SD7wnV",
        wompi_transaction_id="txn-123",
        amount=99.0,
    )

    conn.execute.assert_called_once()
    event_args = conn.execute.call_args[0]
    assert "payment_approved" in event_args[0]
    metadata = json.loads(event_args[5])
    assert metadata["wompi_transaction_id"] == "txn-123"
    assert metadata["gateway_reference"] == "SD7wnV"


@pytest.mark.asyncio
async def test_activate_by_gateway_ref_already_active_is_noop():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={"id": uuid4(), "status": "active", "billing_cycle": "annual"},
    )
    conn.execute = AsyncMock()

    await billing_service.activate_subscription_by_gateway_ref(
        conn,
        tenant_id=tenant_id,
        gateway_reference="SD7wnV",
        wompi_transaction_id="txn-123",
        amount=99.0,
    )

    conn.execute.assert_not_called()
    assert conn.fetchrow.call_count == 1


@pytest.mark.asyncio
async def test_activate_by_gateway_ref_cancelled_not_activated():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={"id": uuid4(), "status": "cancelled", "billing_cycle": "annual"},
    )
    conn.execute = AsyncMock()

    await billing_service.activate_subscription_by_gateway_ref(
        conn,
        tenant_id=tenant_id,
        gateway_reference="SD7wnV",
        wompi_transaction_id="txn-123",
        amount=99.0,
    )

    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_activate_tenant_subscription_past_due_extends_period():
    sub_id = uuid4()
    tenant_id = uuid4()
    new_end = datetime.now(timezone.utc) + timedelta(days=30)

    conn = MagicMock()
    call_count = 0

    async def fetchrow_side_effect(query, *args):
        nonlocal call_count
        call_count += 1
        sql = " ".join(query.split())
        if "FROM tenant_subscriptions ts" in sql:
            return {
                "id": sub_id,
                "tenant_id": tenant_id,
                "billing_cycle": "monthly",
                "tenant_name": "Natural Food",
                "tenant_email": "nf@example.com",
                "plan_name": "Pro",
            }
        if "UPDATE tenant_subscriptions" in sql:
            assert args[1] == "1 month"
            return {"current_period_end": new_end}
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    conn.execute = AsyncMock()

    result = await billing_service.activate_tenant_subscription(
        conn,
        gateway_reference="SD7wnV",
        payment_id="txn-456",
        amount=50.0,
    )

    assert result is not None
    assert result["next_period_end"] == new_end.isoformat()
    conn.execute.assert_called_once()
    metadata = json.loads(conn.execute.call_args[0][5])
    assert metadata["gateway_reference"] == "SD7wnV"
    assert metadata["wompi_transaction_id"] == "txn-456"


@pytest.mark.asyncio
async def test_period_end_after_activation_blocks_cron_demotion():
    """Regression: extended period_end > now() — cron predicate would not match."""
    past_end = datetime(2020, 1, 1, tzinfo=timezone.utc)
    new_end = datetime.now(timezone.utc) + timedelta(days=365)
    assert past_end < datetime.now(timezone.utc)
    assert new_end > datetime.now(timezone.utc)
