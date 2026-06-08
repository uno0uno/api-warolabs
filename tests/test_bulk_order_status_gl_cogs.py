from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import Request

from app.services import orders_service


class _AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_bulk_complete_posts_gl_cogs_only_for_new_completions():
    tenant_id = uuid4()
    user_id = uuid4()
    payment_group_id = uuid4()
    payment_method_id = uuid4()
    pending_order_id = uuid4()
    completed_order_id = uuid4()
    order_date = datetime(2026, 6, 7, 10, 30)

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            None,
            {"id": payment_group_id},
            {"id": payment_method_id},
        ]
    )
    conn.fetch = AsyncMock(
        side_effect=[
            [
                {
                    "id": pending_order_id,
                    "status": "pending",
                    "order_number": 14799,
                    "table_session_id": None,
                    "pos_cart_id": None,
                    "payment_status": None,
                    "total_amount": 25000,
                },
                {
                    "id": completed_order_id,
                    "status": "completed",
                    "order_number": 14800,
                    "table_session_id": None,
                    "pos_cart_id": None,
                    "payment_status": "paid",
                    "total_amount": 30000,
                },
            ],
            [
                {
                    "id": pending_order_id,
                    "order_number": 14799,
                    "total_amount": 25000,
                    "payment_method": "digital",
                    "payment_method_id": payment_method_id,
                    "order_date": order_date,
                },
            ],
        ]
    )
    conn.execute = AsyncMock(return_value="UPDATE 2")

    post_gl = AsyncMock()
    post_cogs = AsyncMock()
    deduct_stock = AsyncMock()

    with patch(
        "app.services.orders_service.require_valid_session",
        return_value=SimpleNamespace(tenant_id=tenant_id, user_id=user_id),
    ), patch(
        "app.services.orders_service.get_db_connection",
        return_value=_AsyncContext(conn),
    ), patch(
        "app.services.orders_service._deduct_stock_for_status_update",
        new=deduct_stock,
    ), patch(
        "app.services.orders_service._get_tenant_tax_config",
        new=AsyncMock(return_value={"inc_enabled": True}),
    ), patch(
        "app.services.orders_service._post_order_gl_entry",
        new=post_gl,
    ), patch(
        "app.services.orders_service._post_order_cogs_gl_entry",
        new=post_cogs,
    ):
        result = await orders_service.bulk_update_order_status(
            Request({"type": "http"}),
            [str(pending_order_id), str(completed_order_id)],
            "completed",
            payment_method="digital",
            payment_method_id=str(payment_method_id),
        )

    assert result["success"] is True
    assert result["updated"] == 2

    deduct_stock.assert_awaited_once_with(
        conn,
        pending_order_id,
        tenant_id,
        user_id,
        14799,
    )
    post_gl.assert_awaited_once()
    assert post_gl.await_args.kwargs["order_id"] == pending_order_id
    assert post_gl.await_args.kwargs["order_date"] == date(2026, 6, 7)
    assert post_gl.await_args.kwargs["payment_method"] == "digital"
    assert post_gl.await_args.kwargs["payment_method_id"] == payment_method_id
    post_cogs.assert_awaited_once_with(
        conn=conn,
        tenant_id=tenant_id,
        order_id=pending_order_id,
        order_date=date(2026, 6, 7),
        order_number=14799,
    )
