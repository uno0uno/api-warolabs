"""Wallet recharge receipt-email endpoint (warocol.com#2549)."""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import Request
from pydantic import ValidationError

from app.routers import customers as customers_router


@pytest.mark.asyncio
async def test_wallet_receipt_email_happy_path():
    request = MagicMock(spec=Request)
    session = MagicMock()
    session.tenant_id = "tenant-uuid"
    customer_id = uuid4()

    body = customers_router.SendWalletRechargeReceiptRequest(
        email="cliente@example.com",
        customer_name="Anderson Arévalo",
        recharge_date="2026-09-01T12:00:00",
        payment_method_label="Efectivo",
        amount_cop=50000.0,
        balance_after_cop=150000.0,
        notes="Recarga en efectivo",
    )

    with (
        patch("app.routers.customers.require_valid_session", return_value=session),
        patch(
            "app.routers.customers.send_wallet_recharge_receipt_email",
            new=AsyncMock(return_value=True),
        ) as send_mock,
    ):
        result = await customers_router.send_wallet_recharge_receipt_email_endpoint(
            request,
            customer_id=customer_id,
            body=body,
        )

    assert result == {"success": True}
    send_mock.assert_awaited_once()
    assert send_mock.await_args.kwargs["customer_email"] == "cliente@example.com"
    assert send_mock.await_args.kwargs["amount_cop"] == 50000.0


def test_wallet_receipt_email_invalid_email_rejected_by_schema():
    with pytest.raises(ValidationError):
        customers_router.SendWalletRechargeReceiptRequest(
            email="not-an-email",
            customer_name="Test",
            recharge_date="2026-09-01",
            payment_method_label="Cash",
            amount_cop=100.0,
            balance_after_cop=100.0,
        )
