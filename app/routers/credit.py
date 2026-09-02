"""
Credit Router
Endpoints for managing credit sales — payment registration, history, and open-credits list.

Issue: https://github.com/uno0uno/warocol.com/issues/294
"""
from fastapi import Depends, APIRouter, Request, Query
from app.core.permissions import Module, require_module
from app.core.middleware import require_valid_session
from typing import List, Optional
from uuid import UUID
from decimal import Decimal
from datetime import date
from pydantic import BaseModel, Field, EmailStr
from app.services import credit_service
from app.services.email_helpers import send_credit_abono_receipt_email

router = APIRouter(prefix="/credit", tags=["credit"])


class CreditReceiptOrderLine(BaseModel):
    order_number: int = Field(..., ge=1)
    amount: float = Field(..., gt=0)
    remaining_amount: float = Field(..., ge=0)


class SendCreditReceiptRequest(BaseModel):
    email: EmailStr = Field(..., description="Recipient email")
    customer_name: str = Field(..., min_length=1)
    payment_date: str = Field(..., description="ISO datetime or date label from client")
    payment_method_label: str = Field(..., min_length=1)
    total_amount: float = Field(..., gt=0)
    lines: List[CreditReceiptOrderLine] = Field(..., min_length=1)
    notes: Optional[str] = None
    total_outstanding_after: Optional[float] = Field(None, ge=0)
    business_name: Optional[str] = None
    business_address: Optional[str] = None
    business_city: Optional[str] = None
    business_phone: Optional[str] = None


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


@router.post("/receipt-email", dependencies=[Depends(require_module(Module.FINANZAS))])
async def send_credit_receipt_email(request: Request, body: SendCreditReceiptRequest):
    """
    Email a credit cartera abono receipt after CRM payment registration.
  """
    session = require_valid_session(request)
    tenant_id = session.tenant_id if session else None

    success = await send_credit_abono_receipt_email(
        customer_email=str(body.email),
        customer_name=body.customer_name,
        payment_date_label=body.payment_date,
        payment_method_label=body.payment_method_label,
        total_amount=body.total_amount,
        lines=[line.model_dump() for line in body.lines],
        notes=body.notes,
        total_outstanding_after=body.total_outstanding_after,
        tenant_id=str(tenant_id) if tenant_id else None,
        business_name=body.business_name,
        business_address=body.business_address,
        business_city=body.business_city,
        business_phone=body.business_phone,
    )
    return {"success": success}


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
