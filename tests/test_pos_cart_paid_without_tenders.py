"""Tests for add_order_payment when payment_status disagrees with recorded tenders (#2531)."""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.exceptions import APIError
from app.services import pos_cart_service


class _AsyncContext:
    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        return None

    def __init__(self, value=None):
        self._value = value


@pytest.mark.asyncio
async def test_add_order_payment_allows_completed_paid_when_no_tenders():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    payment_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {
            "id": order_id,
            "total_amount": 71000.0,
            "tip_amount": 0.0,
            "tip_source": "none",
            "tip_taxable": False,
            "tip_tax_amount": 0.0,
            "status": "completed",
            "payment_status": "paid",
            "customer_id": None,
            "order_number": 19029,
            "table_session_id": None,
        },
        {"paid_total": 0.0},
        {"id": payment_id},
        {"paid_total": 35000.0},
    ])
    conn.fetchval = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncContext())

    with patch("app.services.pos_cart_service.require_valid_session", return_value=MagicMock(tenant_id=tenant_id, user_id=user_id)), \
         patch("app.services.pos_cart_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.pos_cart_service.resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")), \
         patch("app.services.pos_cart_service._get_tenant_tax_config", new=AsyncMock(return_value={})), \
         patch("app.services.pos_cart_service._additive_tax_for_order", new=AsyncMock(return_value=0)):
        result = await pos_cart_service.add_order_payment(
            MagicMock(),
            amount=35000.0,
            payment_method="cash",
            order_id=str(order_id),
        )

    assert result["success"] is True
    assert result["data"]["payment_id"] == str(payment_id)
    assert result["data"]["remaining"] == 36000.0


@pytest.mark.asyncio
async def test_add_order_payment_rejects_when_tenders_cover_balance():
    tenant_id = uuid4()
    user_id = uuid4()
    order_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[
        {
            "id": order_id,
            "total_amount": 71000.0,
            "tip_amount": 0.0,
            "tip_source": "none",
            "tip_taxable": False,
            "tip_tax_amount": 0.0,
            "status": "completed",
            "payment_status": "paid",
            "customer_id": None,
            "order_number": 19029,
            "table_session_id": None,
        },
        {"paid_total": 71000.0},
    ])
    conn.transaction = MagicMock(return_value=_AsyncContext())

    with patch("app.services.pos_cart_service.require_valid_session", return_value=MagicMock(tenant_id=tenant_id, user_id=user_id)), \
         patch("app.services.pos_cart_service.get_db_connection", return_value=_AsyncContext(conn)), \
         patch("app.services.pos_cart_service.resolve_tenant_timezone", new=AsyncMock(return_value="America/Bogota")), \
         patch("app.services.pos_cart_service._get_tenant_tax_config", new=AsyncMock(return_value={})), \
         patch("app.services.pos_cart_service._additive_tax_for_order", new=AsyncMock(return_value=0)):
        with pytest.raises(APIError) as exc:
            await pos_cart_service.add_order_payment(
                MagicMock(),
                amount=100.0,
                payment_method="cash",
                order_id=str(order_id),
            )

    assert exc.value.status_code == 400
    assert "saldo pendiente" in str(exc.value).lower()
