"""Abandon pending checkout → Starter (#2210)."""
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services import billing_service


@pytest.mark.asyncio
async def test_abandon_pending_checkout_deletes_pending_row():
    tenant_id = uuid4()
    sub_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": sub_id,
                "status": "pending",
                "gateway_reference": "txn_pending_1",
                "plan_id": uuid4(),
            },
            {"id": sub_id},
        ]
    )
    conn.execute = AsyncMock()

    result = await billing_service.abandon_pending_checkout(conn, tenant_id)

    assert result == {"status": "abandoned", "gateway_reference": "txn_pending_1"}
    assert conn.execute.await_count == 1
    insert_sql = conn.execute.await_args.args[0]
    assert "checkout_abandoned" in insert_sql
    delete_sql = conn.fetchrow.await_args_list[1].args[0]
    assert "DELETE FROM tenant_subscriptions" in delete_sql


@pytest.mark.asyncio
async def test_abandon_pending_checkout_rejects_active():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": uuid4(),
            "status": "active",
            "gateway_reference": "txn_active",
            "plan_id": uuid4(),
        }
    )

    with pytest.raises(HTTPException) as exc:
        await billing_service.abandon_pending_checkout(conn, uuid4())

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_abandon_pending_checkout_404_when_missing():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)

    with pytest.raises(HTTPException) as exc:
        await billing_service.abandon_pending_checkout(conn, uuid4())

    assert exc.value.status_code == 404
