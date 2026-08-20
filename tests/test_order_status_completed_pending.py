"""Completed sales cannot return to pending; line edits only while pending."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import Request

from app.core.exceptions import APIError
from app.services import orders_service


class _AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _session(tenant_id, user_id):
    return SimpleNamespace(tenant_id=tenant_id, user_id=user_id)


def _completed_mesa_row(order_id):
    return {
        "id": order_id,
        "status": "completed",
        "order_number": 18401,
        "table_session_id": uuid4(),
        "pos_cart_id": None,
        "payment_status": "paid",
        "order_date": datetime(2026, 8, 15),
        "total_amount": 45000,
        "customer_id": None,
    }


@pytest.mark.asyncio
async def test_update_order_status_rejects_completed_to_pending_for_mesa():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=_completed_mesa_row(order_id))
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    with patch("app.services.orders_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
         patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.orders_service.resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")), \
         patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()):
        with pytest.raises(APIError) as exc:
            await orders_service.update_order_status(
                Request({"type": "http"}),
                order_id,
                "pending",
            )

    assert exc.value.status_code == 400
    assert "pendiente" in exc.value.message
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_bulk_update_rejects_completed_to_pending_without_pos_cart():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value="America/Bogota")
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[
        {
            "id": order_id,
            "status": "completed",
            "order_number": 18401,
            "table_session_id": uuid4(),
            "pos_cart_id": None,
            "payment_status": "paid",
            "total_amount": 45000,
        },
    ])
    conn.execute = AsyncMock()

    with patch("app.services.orders_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
         patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)):
        with pytest.raises(APIError) as exc:
            await orders_service.bulk_update_order_status(
                Request({"type": "http"}),
                [str(order_id)],
                "pending",
            )

    assert exc.value.status_code == 400
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_delete_order_item_rejects_when_completed():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    item_id = uuid4()
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())
    conn.fetchrow = AsyncMock(return_value={
        "id": order_id,
        "order_number": 42,
        "order_date": datetime(2026, 8, 15),
        "status": "completed",
    })
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()

    with patch("app.services.orders_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
         patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()):
        with pytest.raises(APIError) as exc:
            await orders_service.delete_order_item(Request({"type": "http"}), order_id, item_id)

    assert exc.value.status_code == 409
    assert exc.value.details.get("code") == "sale_not_editable"
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_delete_order_item_rejects_when_pending():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    item_id = uuid4()
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())
    conn.fetchrow = AsyncMock(return_value={
        "id": order_id,
        "order_number": 42,
        "order_date": datetime(2026, 8, 15),
        "status": "pending",
    })
    conn.execute = AsyncMock()

    with patch("app.services.orders_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
         patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()), \
         patch("app.services.orders_service.assert_order_invoice_allows_mutation", new=AsyncMock()):
        with pytest.raises(APIError) as exc:
            await orders_service.delete_order_item(Request({"type": "http"}), order_id, item_id)

    assert exc.value.status_code == 409
    assert exc.value.details.get("code") == "sale_not_editable"
    conn.execute.assert_not_called()
