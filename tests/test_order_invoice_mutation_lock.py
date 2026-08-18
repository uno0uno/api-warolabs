"""Block order mutations when the latest electronic invoice is pending or accepted."""
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


def _order_row(order_id):
    return {
        "id": order_id,
        "status": "completed",
        "order_number": 18402,
        "table_session_id": uuid4(),
        "pos_cart_id": None,
        "payment_status": "paid",
        "order_date": datetime(2026, 8, 15),
        "total_amount": 45000,
        "customer_id": None,
    }


@pytest.mark.asyncio
async def test_assert_order_invoice_allows_mutation_blocks_pending_and_accepted():
    conn = AsyncMock()
    order_id = uuid4()
    tenant_id = uuid4()
    for status in ("pending", "accepted"):
        conn.fetchval = AsyncMock(return_value=status)
        with pytest.raises(APIError) as exc:
            await orders_service.assert_order_invoice_allows_mutation(conn, tenant_id, order_id)
        assert exc.value.status_code == 409
        assert "factura electrónica" in exc.value.message


@pytest.mark.asyncio
async def test_assert_order_invoice_allows_mutation_allows_rejected_and_missing():
    conn = AsyncMock()
    order_id = uuid4()
    tenant_id = uuid4()
    for status in ("rejected", None):
        conn.fetchval = AsyncMock(return_value=status)
        await orders_service.assert_order_invoice_allows_mutation(conn, tenant_id, order_id)


@pytest.mark.asyncio
async def test_update_order_status_rejects_when_invoice_accepted():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=_order_row(order_id))
    conn.fetchval = AsyncMock(return_value="accepted")
    conn.execute = AsyncMock()

    with patch("app.services.orders_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
         patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.orders_service.resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")), \
         patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()):
        with pytest.raises(APIError) as exc:
            await orders_service.update_order_status(
                Request({"type": "http"}),
                order_id,
                "cancelled",
            )

    assert exc.value.status_code == 409
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_delete_order_item_rejects_when_invoice_pending():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    item_id = uuid4()
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())
    conn.fetchrow = AsyncMock(return_value={
        "id": order_id, "order_number": 42, "order_date": datetime(2026, 8, 15),
    })
    conn.fetchval = AsyncMock(return_value="pending")
    conn.execute = AsyncMock()

    with patch("app.services.orders_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
         patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()):
        with pytest.raises(APIError) as exc:
            await orders_service.delete_order_item(Request({"type": "http"}), order_id, item_id)

    assert exc.value.status_code == 409
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_delete_order_item_modifier_rejects_when_invoice_accepted():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    item_id = uuid4()
    modifier_id = uuid4()
    conn = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())
    conn.fetchrow = AsyncMock(return_value={
        "id": order_id, "order_number": 42, "order_date": datetime(2026, 8, 15),
    })
    conn.fetchval = AsyncMock(return_value="accepted")
    conn.execute = AsyncMock()

    with patch("app.services.orders_service.require_valid_session", return_value=_session(tenant_id, user_id)), \
         patch("app.services.orders_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.orders_service.assert_order_not_in_closed_monthly_period", new=AsyncMock()):
        with pytest.raises(APIError) as exc:
            await orders_service.delete_order_item_modifier(
                Request({"type": "http"}),
                order_id,
                item_id,
                modifier_id,
            )

    assert exc.value.status_code == 409
    conn.execute.assert_not_called()
