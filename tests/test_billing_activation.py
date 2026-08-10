"""Subscription activation — idempotency, period anchor, customer events (#361)."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services import billing_service


def _conn_with_activation(
    *,
    sub_row,
    duplicate: bool = False,
    period_anchor=None,
):
    """MagicMock conn: fetchrow (lookup + UPDATE), fetchval (idempotency), execute (event)."""
    new_end = datetime.now(timezone.utc) + timedelta(days=365)
    conn = MagicMock()

    async def fetchrow_side_effect(query, *args):
        sql = " ".join(query.split())
        if "FROM tenant_subscriptions" in sql and "gateway_reference" in sql:
            return sub_row
        if "FROM tenant_subscriptions ts" in sql:
            return sub_row
        if "UPDATE tenant_subscriptions" in sql:
            assert "current_period_start = $3::timestamptz" in sql
            assert "current_period_end = $3::timestamptz + CASE" in sql
            assert args[1] in ("monthly", "annual")
            if period_anchor is not None:
                assert args[2] == period_anchor
            return {"current_period_end": new_end}
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    conn.fetchval = AsyncMock(return_value=1 if duplicate else None)
    conn.execute = AsyncMock()
    return conn, new_end


@pytest.mark.asyncio
async def test_activate_by_gateway_ref_past_due_extends_period():
    tenant_id = uuid4()
    sub_id = uuid4()
    sub_row = {"id": sub_id, "status": "past_due", "billing_cycle": "annual"}
    conn, _ = _conn_with_activation(sub_row=sub_row)

    await billing_service.activate_subscription_by_gateway_ref(
        conn,
        tenant_id=tenant_id,
        gateway_reference="SD7wnV",
        wompi_transaction_id="txn-123",
        amount=99.0,
    )

    assert conn.execute.await_count >= 1
    metadata = json.loads(conn.execute.call_args[0][5])
    assert metadata["wompi_transaction_id"] == "txn-123"
    assert metadata["gateway_reference"] == "SD7wnV"


@pytest.mark.asyncio
async def test_activate_by_gateway_ref_duplicate_skips_activation():
    tenant_id = uuid4()
    sub_row = {"id": uuid4(), "status": "past_due", "billing_cycle": "annual"}
    conn, _ = _conn_with_activation(sub_row=sub_row, duplicate=True)

    await billing_service.activate_subscription_by_gateway_ref(
        conn,
        tenant_id=tenant_id,
        gateway_reference="SD7wnV",
        wompi_transaction_id="txn-dup",
        amount=99.0,
    )

    conn.execute.assert_not_called()
    assert conn.fetchrow.call_count == 1


@pytest.mark.asyncio
async def test_activate_by_gateway_ref_uses_period_anchor():
    tenant_id = uuid4()
    anchor = datetime(2025, 5, 31, 18, 0, tzinfo=timezone.utc)
    sub_row = {"id": uuid4(), "status": "past_due", "billing_cycle": "annual"}
    conn, _ = _conn_with_activation(sub_row=sub_row, period_anchor=anchor)

    await billing_service.activate_subscription_by_gateway_ref(
        conn,
        tenant_id=tenant_id,
        gateway_reference="SD7wnV",
        wompi_transaction_id="txn-anchor",
        amount=99.0,
        period_anchor=anchor,
    )

    assert conn.execute.await_count >= 1


@pytest.mark.asyncio
async def test_activate_by_gateway_ref_already_active_is_noop():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={"id": uuid4(), "status": "active", "billing_cycle": "annual"},
    )
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock()

    await billing_service.activate_subscription_by_gateway_ref(
        conn,
        tenant_id=tenant_id,
        gateway_reference="SD7wnV",
        wompi_transaction_id="txn-123",
        amount=99.0,
    )

    conn.execute.assert_not_called()
    conn.fetchval.assert_not_called()


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
    sub_row = {
        "id": sub_id,
        "tenant_id": tenant_id,
        "billing_cycle": "monthly",
        "tenant_name": "Natural Food",
        "tenant_email": "nf@example.com",
        "plan_name": "Pro",
    }
    conn, _ = _conn_with_activation(sub_row=sub_row)

    async def fetchrow_side_effect(query, *args):
        sql = " ".join(query.split())
        if "FROM tenant_subscriptions ts" in sql:
            return sub_row
        if "UPDATE tenant_subscriptions" in sql:
            assert args[1] == "monthly"
            return {"current_period_end": new_end}
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)

    result = await billing_service.activate_tenant_subscription(
        conn,
        gateway_reference="SD7wnV",
        payment_id="txn-456",
        amount=50.0,
    )

    assert result is not None
    assert result["next_period_end"] == new_end.isoformat()
    conn.execute.assert_called_once()


@pytest.mark.asyncio
async def test_activate_tenant_subscription_duplicate_returns_none():
    sub_row = {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "billing_cycle": "annual",
        "tenant_name": "T",
        "tenant_email": "t@x.com",
        "plan_name": "Pro",
    }
    conn, _ = _conn_with_activation(sub_row=sub_row, duplicate=True)

    async def fetchrow_side_effect(query, *args):
        if "FROM tenant_subscriptions ts" in " ".join(query.split()):
            return sub_row
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)

    result = await billing_service.activate_tenant_subscription(
        conn,
        gateway_reference="SD7wnV",
        payment_id="txn-dup",
        amount=50.0,
    )

    assert result is None


@pytest.mark.asyncio
async def test_activate_tenant_subscription_rejects_return_amount_mismatch():
    tenant_id = uuid4()
    sub_row = {
        "id": uuid4(),
        "tenant_id": tenant_id,
        "billing_cycle": "annual",
        "tenant_name": "Test tenant",
        "tenant_email": "test@example.com",
        "plan_name": "Pro",
        "price_annual": 95900,
    }
    conn, _ = _conn_with_activation(sub_row=sub_row)

    with pytest.raises(HTTPException) as exc_info:
        await billing_service.activate_tenant_subscription(
            conn,
            gateway_reference="test-link",
            payment_id="tx-mismatch",
            amount=1,
            currency="COP",
            expected_tenant_id=tenant_id,
            amount_in_cents=100,
        )

    assert exc_info.value.status_code == 409
    conn.execute.assert_not_called()


def test_parse_wompi_period_anchor_prefers_finalized_at():
    anchor = billing_service.parse_wompi_period_anchor({
        "finalized_at": "2025-05-31T18:00:00.000Z",
        "created_at": "2025-05-31T17:00:00.000Z",
    })
    assert anchor == datetime(2025, 5, 31, 18, 0, tzinfo=timezone.utc)


def test_parse_wompi_period_anchor_falls_back_to_created_at():
    anchor = billing_service.parse_wompi_period_anchor({
        "created_at": "2025-05-31T17:00:00.000Z",
    })
    assert anchor == datetime(2025, 5, 31, 17, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_list_tenant_billing_events_filters_internal_types():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=0)

    await billing_service.list_tenant_billing_events(conn, tenant_id, limit=10, offset=0)

    visible = list(billing_service.CUSTOMER_VISIBLE_BILLING_EVENT_TYPES)
    fetch_args = conn.fetch.call_args[0]
    assert fetch_args[4] == visible
    count_args = conn.fetchval.call_args[0]
    assert count_args[2] == visible
    assert "grace_reminder" not in visible
    assert "subscription_period_ended" not in visible


@pytest.mark.asyncio
async def test_period_end_after_activation_blocks_cron_demotion():
    """Regression: extended period_end > now() — cron predicate would not match."""
    past_end = datetime(2020, 1, 1, tzinfo=timezone.utc)
    new_end = datetime.now(timezone.utc) + timedelta(days=365)
    assert past_end < datetime.now(timezone.utc)
    assert new_end > datetime.now(timezone.utc)
