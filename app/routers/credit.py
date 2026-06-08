"""
Credit Router
Endpoints for managing credit sales — payment registration, history, and open-credits list.

Issue: https://github.com/uno0uno/warocol.com/issues/294
"""
from fastapi import Depends, APIRouter, Request, Query
from app.core.permissions import Module, require_module
from typing import Optional
from uuid import UUID
from decimal import Decimal
from datetime import date
from pydantic import BaseModel, Field
from app.services import credit_service

router = APIRouter(prefix="/credit", tags=["credit"])


class RegisterCreditPaymentRequest(BaseModel):
    amount: Decimal = Field(..., gt=0, description="Payment amount (must be > 0)")
    payment_method: str = Field(..., description="Payment method group slug")
    payment_method_id: Optional[UUID] = Field(
        None,
        description="UUID of the selected payment_methods row",
    )
    notes: Optional[str] = Field(None, description="Optional notes for this payment")
    payment_date: Optional[date] = Field(None, description="Payment date (defaults to now)")


@router.post("/orders/{order_id}/payments", dependencies=[Depends(require_module(Module.FINANZAS))])
async def register_payment(
    request: Request,
    order_id: UUID,
    body: RegisterCreditPaymentRequest,
):
    """
    Register a partial or full payment against a credit order.

    - order must have payment_status = 'credit' or 'partial'
    - amount must not exceed (total_amount - credit_paid_amount)
    - updates credit_paid_amount and transitions payment_status automatically:
        credit -> partial (partial payment)
        partial -> paid   (balance cleared)
    """
    return await credit_service.register_credit_payment(
        request,
        order_id,
        body.amount,
        body.payment_method,
        body.payment_method_id,
        body.notes,
        body.payment_date,
    )


@router.get("/orders/{order_id}/payments", dependencies=[Depends(require_module(Module.FINANZAS))])
async def get_payments(
    request: Request,
    order_id: UUID,
):
    """
    List all credit payment records for a specific order.
    Returns order credit summary (total, paid, remaining) + payments list.
    """
    return await credit_service.get_credit_payments(request, order_id)


@router.get("/", dependencies=[Depends(require_module(Module.FINANZAS))])
async def list_open_credits(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """
    List all open credit orders for the tenant (payment_status IN ('credit', 'partial')).
    Used by the Cartera view.
    """
    return await credit_service.list_credit_orders(request, limit, offset)
