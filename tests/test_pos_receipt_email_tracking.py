"""POS receipt-email tracking registration (warocol.com#1769)."""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import Request

from app.routers import pos_cart as pos_cart_router


_TENANT_ID = UUID("93b3e582-34fa-44a6-8d0f-bf82a3608727")
_ORDER_ID = uuid4()
_DELIVERY_ID = uuid4()


class _OwnedConnCtx:
    def __init__(self, owned_row):
        self._owned = owned_row

    async def __aenter__(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=self._owned)
        return conn

    async def __aexit__(self, *_):
        return False


@pytest.mark.asyncio
async def test_receipt_email_registers_delivery_when_order_id_owned():
    request = MagicMock(spec=Request)
    session = MagicMock()
    session.tenant_id = _TENANT_ID

    body = pos_cart_router.SendReceiptRequest(
        email="cashier@example.com",
        order_number=42,
        order_id=_ORDER_ID,
        total_amount=1000.0,
        payment_method="cash",
        items=[],
    )

    with (
        patch("app.routers.pos_cart.require_valid_session", return_value=session),
        patch(
            "app.routers.pos_cart.get_db_connection",
            lambda *a, **k: _OwnedConnCtx({"id": _ORDER_ID}),
        ),
        patch(
            "app.routers.pos_cart.invoice_email_tracking_service.create_pending_delivery",
            new=AsyncMock(return_value=_DELIVERY_ID),
        ) as create_pending,
        patch(
            "app.routers.pos_cart.invoice_email_tracking_service.mark_delivery_sent",
            new=AsyncMock(),
        ) as mark_sent,
        patch(
            "app.routers.pos_cart.invoice_email_tracking_service.mark_delivery_failed",
            new=AsyncMock(),
        ) as mark_failed,
        patch(
            "app.routers.pos_cart.send_pos_receipt_email",
            new=AsyncMock(return_value=True),
        ) as send_mock,
    ):
        result = await pos_cart_router.send_receipt_email(request, body)

    assert result == {"success": True}
    create_pending.assert_awaited_once()
    assert create_pending.await_args.kwargs["order_id"] == _ORDER_ID
    assert create_pending.await_args.kwargs["tenant_id"] == _TENANT_ID
    send_mock.assert_awaited_once()
    assert send_mock.await_args.kwargs["tracking_pixel_url"]
    mark_sent.assert_awaited_once_with(_DELIVERY_ID)
    mark_failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_receipt_email_skips_tracking_without_order_id():
    request = MagicMock(spec=Request)
    session = MagicMock()
    session.tenant_id = _TENANT_ID

    body = pos_cart_router.SendReceiptRequest(
        email="cashier@example.com",
        order_number=42,
        total_amount=1000.0,
        payment_method="cash",
        items=[],
    )

    with (
        patch("app.routers.pos_cart.require_valid_session", return_value=session),
        patch(
            "app.routers.pos_cart.invoice_email_tracking_service.create_pending_delivery",
            new=AsyncMock(),
        ) as create_pending,
        patch(
            "app.routers.pos_cart.send_pos_receipt_email",
            new=AsyncMock(return_value=True),
        ) as send_mock,
    ):
        result = await pos_cart_router.send_receipt_email(request, body)

    assert result == {"success": True}
    create_pending.assert_not_awaited()
    assert send_mock.await_args.kwargs.get("tracking_pixel_url") is None
