"""Customer credit payment history list (warocol.com#2548)."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services import cartera_service


def _request():
    return MagicMock()


def _session(tenant_id):
    session = MagicMock()
    session.tenant_id = tenant_id
    return session


def _db_context(conn):
    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx


@pytest.mark.asyncio
async def test_list_customer_credit_payments_returns_paginated_items():
    tenant_id = uuid4()
    customer_id = uuid4()
    payment_id = uuid4()
    order_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": customer_id})
    conn.fetchval = AsyncMock(return_value=1)
    conn.fetch = AsyncMock(
        return_value=[
            {
                "id": payment_id,
                "order_id": order_id,
                "amount": 21000.0,
                "payment_method": "cash",
                "payment_method_id": None,
                "payment_date": datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
                "notes": "Pago parcial",
                "created_at": datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
                "order_number": 19807,
                "order_total_amount": 50000.0,
                "remaining_amount_after": 29000.0,
            },
        ],
    )

    with (
        patch(
            "app.services.cartera_service.require_valid_session",
            return_value=_session(tenant_id),
        ),
        patch(
            "app.services.cartera_service.get_db_connection",
            side_effect=_db_context(conn),
        ),
    ):
        result = await cartera_service.list_customer_credit_payments(
            _request(),
            customer_id,
            page=1,
            per_page=10,
        )

    assert result["success"] is True
    data = result["data"]
    assert data["total"] == 1
    assert data["page"] == 1
    assert data["per_page"] == 10
    item = data["items"][0]
    assert item["payment_id"] == str(payment_id)
    assert item["order_number"] == 19807
    assert item["amount"] == 21000.0
    assert item["remaining_amount_after"] == 29000.0
    assert item["notes"] == "Pago parcial"


@pytest.mark.asyncio
async def test_list_customer_credit_payments_customer_not_found():
    tenant_id = uuid4()
    customer_id = uuid4()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)

    with (
        patch(
            "app.services.cartera_service.require_valid_session",
            return_value=_session(tenant_id),
        ),
        patch(
            "app.services.cartera_service.get_db_connection",
            side_effect=_db_context(conn),
        ),
    ):
        from app.core.exceptions import APIError

        with pytest.raises(APIError) as exc:
            await cartera_service.list_customer_credit_payments(
                _request(),
                customer_id,
            )

    assert exc.value.status_code == 404
