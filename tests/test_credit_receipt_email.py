"""Credit cartera abono receipt-email endpoint (warocol.com#2545)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request

from app.routers import credit as credit_router


@pytest.mark.asyncio
async def test_credit_receipt_email_happy_path():
    request = MagicMock(spec=Request)
    session = MagicMock()
    session.tenant_id = "tenant-uuid"

    body = credit_router.SendCreditReceiptRequest(
        email="cliente@example.com",
        customer_name="Anderson Arévalo",
        payment_date="2026-09-01T12:00:00",
        payment_method_label="Efectivo",
        total_amount=21000.0,
        lines=[
            credit_router.CreditReceiptOrderLine(
                order_number=19807,
                amount=21000.0,
                remaining_amount=0.0,
            ),
        ],
        total_outstanding_after=872880.0,
    )

    with (
        patch("app.routers.credit.require_valid_session", return_value=session),
        patch(
            "app.routers.credit.send_credit_abono_receipt_email",
            new=AsyncMock(return_value=True),
        ) as send_mock,
    ):
        result = await credit_router.send_credit_receipt_email(request, body)

    assert result == {"success": True}
    send_mock.assert_awaited_once()
    assert send_mock.await_args.kwargs["customer_email"] == "cliente@example.com"
    assert send_mock.await_args.kwargs["total_amount"] == 21000.0


@pytest.mark.asyncio
async def test_credit_receipt_email_invalid_email_rejected_by_schema():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        credit_router.SendCreditReceiptRequest(
            email="not-an-email",
            customer_name="Test",
            payment_date="2026-09-01",
            payment_method_label="Cash",
            total_amount=100.0,
            lines=[
                credit_router.CreditReceiptOrderLine(
                    order_number=1,
                    amount=100.0,
                    remaining_amount=0.0,
                ),
            ],
        )
