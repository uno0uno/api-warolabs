"""Grandfather mid-period gate + post-end monthly renew (#797 / #809)."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.core.billing_pricing import is_grandfathered_annual
from app.services import billing_service


def test_is_grandfathered_annual_requires_active_annual_future_end():
    future = datetime.now(timezone.utc) + timedelta(days=30)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    assert is_grandfathered_annual(
        status="active", billing_cycle="annual", current_period_end=future,
    )
    assert not is_grandfathered_annual(
        status="past_due", billing_cycle="annual", current_period_end=future,
    )
    assert not is_grandfathered_annual(
        status="active", billing_cycle="monthly", current_period_end=future,
    )
    assert not is_grandfathered_annual(
        status="active", billing_cycle="annual", current_period_end=past,
    )


@pytest.mark.asyncio
async def test_ensure_subscribe_allowed_blocks_grandfathered():
    tenant_id = uuid4()
    future = datetime.now(timezone.utc) + timedelta(days=90)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "status": "active",
            "billing_cycle": "annual",
            "current_period_end": future,
        }
    )
    with pytest.raises(HTTPException) as exc:
        await billing_service.ensure_subscribe_allowed(conn, tenant_id)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "grandfather_active_period"


@pytest.mark.asyncio
async def test_ensure_subscribe_allowed_permits_past_due():
    tenant_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "status": "past_due",
            "billing_cycle": "annual",
            "current_period_end": datetime.now(timezone.utc) - timedelta(days=2),
        }
    )
    await billing_service.ensure_subscribe_allowed(conn, tenant_id)


@pytest.mark.asyncio
async def test_subscribe_tenant_refuses_grandfathered_overwrite():
    tenant_id = uuid4()
    future = datetime.now(timezone.utc) + timedelta(days=60)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "status": "active",
            "billing_cycle": "annual",
            "current_period_end": future,
        }
    )
    conn.execute = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await billing_service.subscribe_tenant(
            conn,
            tenant_id=tenant_id,
            plan_id=uuid4(),
            billing_cycle="monthly",
            checkout_url="https://checkout.test",
            gateway_reference="txn_new",
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "grandfather_active_period"
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_ls_renew_by_tenant_converts_ended_annual_to_monthly():
    tenant_id = uuid4()
    sub_id = uuid4()
    past = datetime.now(timezone.utc) - timedelta(days=3)
    new_end = datetime.now(timezone.utc) + timedelta(days=30)
    conn = MagicMock()
    tenant_row = {
        "id": sub_id,
        "status": "active",
        "billing_cycle": "annual",
        "current_period_end": past,
        "gateway_reference": "txn_old",
    }

    async def fetchrow_side_effect(query, *args):
        sql = " ".join(query.split())
        if "AND gateway_reference" in sql:
            return None
        if "UPDATE tenant_subscriptions" in sql and "current_period_end" in sql:
            assert "billing_cycle" in sql
            assert args[1] == "monthly"
            return {"current_period_end": new_end}
        if "FROM tenant_subscriptions" in sql:
            return tenant_row
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    activated = await billing_service.activate_subscription_by_gateway_ref(
        conn,
        tenant_id=tenant_id,
        gateway_reference="ls_chk_new",
        amount=9.0,
        currency="USD",
        ls_order_id="301",
        provider="lemon_squeezy",
        provider_environment="test",
    )

    assert activated is True
    assert conn.execute.await_count >= 2
    event_call = None
    for call in conn.execute.await_args_list:
        if len(call.args) > 5 and "payment_approved" in str(call.args[0]):
            event_call = call
            break
    assert event_call is not None
    metadata = json.loads(event_call.args[5])
    assert metadata["ls_order_id"] == "301"
    assert metadata["renewal"] is True
    assert metadata["previous_gateway_reference"] == "txn_old"
    assert metadata["converted_from_annual"] is True


@pytest.mark.asyncio
async def test_ls_renew_past_due_annual_converts_to_monthly():
    tenant_id = uuid4()
    sub_id = uuid4()
    past = datetime.now(timezone.utc) - timedelta(days=10)
    new_end = datetime.now(timezone.utc) + timedelta(days=30)
    conn = MagicMock()
    sub_row = {
        "id": sub_id,
        "status": "past_due",
        "billing_cycle": "annual",
        "current_period_end": past,
        "gateway_reference": "txn_past_due",
    }

    async def fetchrow_side_effect(query, *args):
        sql = " ".join(query.split())
        if "FROM tenant_subscriptions" in sql and "gateway_reference" in sql:
            return sub_row
        if "UPDATE tenant_subscriptions" in sql and "current_period_end" in sql:
            assert args[1] == "monthly"
            return {"current_period_end": new_end}
        return None

    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    activated = await billing_service.activate_subscription_by_gateway_ref(
        conn,
        tenant_id=tenant_id,
        gateway_reference="txn_past_due",
        amount=9.0,
        currency="USD",
        ls_order_id="302",
        provider="lemon_squeezy",
    )

    assert activated is True
    metadata = json.loads(conn.execute.call_args[0][5])
    assert metadata["converted_from_annual"] is True


@pytest.mark.asyncio
async def test_ls_renew_skips_grandfathered_mid_period():
    tenant_id = uuid4()
    future = datetime.now(timezone.utc) + timedelta(days=100)
    conn = MagicMock()

    async def fetchrow_side_effect(query, *args):
        sql = " ".join(query.split())
        if "gateway_reference" in sql and "FROM tenant_subscriptions" in sql:
            return None
        return {
            "id": uuid4(),
            "status": "active",
            "billing_cycle": "annual",
            "current_period_end": future,
            "gateway_reference": "txn_old",
        }

    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    conn.fetchval = AsyncMock()
    conn.execute = AsyncMock()

    activated = await billing_service.activate_subscription_by_gateway_ref(
        conn,
        tenant_id=tenant_id,
        gateway_reference="txn_mid_period",
        amount=90.0,
        currency="USD",
        ls_order_id="303",
        provider="lemon_squeezy",
    )

    assert activated is False
    conn.execute.assert_not_called()
