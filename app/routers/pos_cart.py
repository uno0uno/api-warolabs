"""
POS Cart Router
Endpoints for managing POS cart persistence
"""
from fastapi import APIRouter, Depends, Request, Query
from typing import List, Optional, Any, Dict, Literal
from uuid import UUID
from datetime import date, datetime
from pydantic import BaseModel, EmailStr, Field
from app.services import pos_cart_service
from app.services.email_helpers import send_pos_receipt_email
from app.core.middleware import require_valid_session
from app.core.permissions import Module, require_module

router = APIRouter(prefix="/pos/cart", tags=["POS Cart"])


class ModifierInput(BaseModel):
    id: UUID
    name: str
    price: float


class AddItemRequest(BaseModel):
    product_id: UUID
    quantity: int = Field(gt=0)
    unit_price: float
    modifiers: List[ModifierInput] = []
    notes: Optional[str] = None


class BatchAddItemsRequest(BaseModel):
    items: List[AddItemRequest]
    customer_id: Optional[UUID] = None  # Opcional - si no se pasa, se crea carrito anónimo


@router.post("/batch", dependencies=[Depends(require_module(Module.POS))])
async def create_cart_with_items_batch(
    request: Request,
    batch: BatchAddItemsRequest
):
    """
    Create cart and add all items in batch (single transaction).
    - If customer_id is provided, cart is linked to customer
    - If customer_id is None, creates anonymous cart (customer assigned at complete)
    """
    return await pos_cart_service.create_cart_with_batch_items(
        request,
        batch.customer_id,
        [item.dict() for item in batch.items]
    )


@router.get("/{customer_id}", dependencies=[Depends(require_module(Module.POS))])
async def get_cart(
    request: Request,
    customer_id: UUID,
    session_id: Optional[str] = None
):
    """
    Get or create active cart for customer
    """
    return await pos_cart_service.get_or_create_active_cart(
        request,
        customer_id,
        session_id
    )


@router.post("/{cart_id}/items", dependencies=[Depends(require_module(Module.POS))])
async def add_item(
    request: Request,
    cart_id: UUID,
    item: AddItemRequest
):
    """
    Add item to cart
    """
    return await pos_cart_service.add_item_to_cart(
        request,
        cart_id,
        item.product_id,
        item.quantity,
        item.unit_price,
        [mod.dict() for mod in item.modifiers],
        item.notes
    )


class UpdateItemRequest(BaseModel):
    quantity: int = Field(gt=0)
    unit_price: float
    modifiers: List[ModifierInput] = []
    notes: Optional[str] = None


@router.put("/{cart_id}/items/{item_id}", dependencies=[Depends(require_module(Module.POS))])
async def update_item(
    request: Request,
    cart_id: UUID,
    item_id: UUID,
    item: UpdateItemRequest
):
    """
    Update cart item (quantity, modifiers, notes)
    """
    return await pos_cart_service.update_cart_item(
        request,
        cart_id,
        item_id,
        item.quantity,
        item.unit_price,
        [mod.dict() for mod in item.modifiers],
        item.notes
    )


@router.delete("/{cart_id}/items/{item_id}", dependencies=[Depends(require_module(Module.POS))])
async def remove_item(
    request: Request,
    cart_id: UUID,
    item_id: UUID
):
    """
    Remove item from cart
    """
    return await pos_cart_service.remove_item_from_cart(
        request,
        cart_id,
        item_id
    )


@router.delete("/{cart_id}", dependencies=[Depends(require_module(Module.POS))])
async def clear_cart(
    request: Request,
    cart_id: UUID
):
    """
    Clear all items from cart
    """
    return await pos_cart_service.clear_cart(request, cart_id)


class CompleteOrderRequest(BaseModel):
    payment_method: str = Field(..., description="Payment method: cash, card, digital, credit")
    customer_id: UUID = Field(..., description="Customer ID to associate with the order")
    credit_due_date: Optional[date] = Field(None, description="Optional due date for credit orders (only used when payment_method='credit')")
    payment_method_id: Optional[UUID] = Field(None, description="UUID of the selected payment_methods row (nullable if group-level only)")
    receipt_email: Optional[EmailStr] = Field(None, description="Optional customer email to send receipt to after order completes")
    discount_type: Optional[str] = Field(None, description="'percent' | 'fixed'")
    discount_value: Optional[float] = Field(None, description="10 for 10%, 5000 for $5,000 COP")
    split_mode: bool = Field(False, description="True when using split payment — creates order with payment_status='partial'")
    split_first_amount: float = Field(0.0, description="Amount for the first split payment (used only when split_mode=True)")
    split_first_cash_received: Optional[float] = Field(None, description="Issue #524 — cash handed over by the customer for the first split payment. NULL for non-cash methods. Must be >= split_first_amount when set.")
    cash_received: Optional[float] = Field(None, description="Issue #524 — cash handed over for a single (non-split) cash payment. NULL for non-cash methods. Must be >= total_amount.")
    table_session_id: Optional[UUID] = Field(None, description="Bar/table session ID — links the order to a session for source tracking")
    delivery_address_id: Optional[UUID] = Field(None, description="UUID of an addresses_profile row owned by customer_id. When set, this order is treated as a delivery and the comanda fires with source_type='delivery'.")
    scheduled_time: Optional[datetime] = Field(None, description="ISO datetime for scheduled delivery. NULL = ASAP. Forward-compatible: v1 UI sends NULL only.")
    delivery_instructions: Optional[str] = Field(None, description="Free-text notes for the courier (e.g. 'Tocar el timbre 2 veces').")
    served_by_member_id: Optional[UUID] = Field(None, description="Issue warocol.com#575/#663 — waiter at checkout. Validated against active tenant members; persisted even when waiter_attribution_enabled is OFF.")
    tip_amount: float = Field(0, ge=0, description="Issue warocol.com#637 — tip amount in COP. Strictly separate from total_amount. Rejected when tip_enabled=false for the tenant. Rejected when split_mode=true. For cash payments, cash_received must cover total + tip.")
    tip_source: Literal['preset', 'custom', 'none'] = Field('none', description="Issue warocol.com#637 — how the customer chose the tip. Must agree with tip_amount: (0,'none') or (>0,'preset'|'custom').")


@router.post("/{cart_id}/complete", dependencies=[Depends(require_module(Module.POS))])
async def complete_order(
    request: Request,
    cart_id: UUID,
    order_data: CompleteOrderRequest
):
    """
    Complete POS order:
    - Associates customer with cart (if not already)
    - Creates order record
    - Copies cart items to order_items
    - Updates inventory
    - Marks cart as completed
    """
    return await pos_cart_service.complete_pos_order(
        request,
        cart_id,
        order_data.payment_method,
        order_data.customer_id,
        order_data.credit_due_date,
        order_data.payment_method_id,
        order_data.receipt_email,
        order_data.discount_type,
        order_data.discount_value,
        order_data.split_mode,
        order_data.split_first_amount,
        order_data.table_session_id,
        order_data.delivery_address_id,
        order_data.scheduled_time,
        order_data.delivery_instructions,
        split_first_cash_received=order_data.split_first_cash_received,
        cash_received=order_data.cash_received,
        served_by_member_id=order_data.served_by_member_id,
        tip_amount=order_data.tip_amount,
        tip_source=order_data.tip_source,
    )


@router.post("/{cart_id}/fire", dependencies=[Depends(require_module(Module.POS))])
async def fire_pos_cart(request: Request, cart_id: UUID):
    """
    Explicitly fire all 'new' items in a POS cart/order to the kitchen.
    Returns comanda summaries and fired item count.
    """
    return await pos_cart_service.fire_pos_cart(request, cart_id)


class SendReceiptRequest(BaseModel):
    email: EmailStr = Field(..., description="Customer email to send receipt to")
    order_number: int
    total_amount: float
    payment_method: str
    items: List[Dict[str, Any]] = Field(default_factory=list)
    business_name: Optional[str] = None
    business_address: Optional[str] = None
    business_city: Optional[str] = None
    business_phone: Optional[str] = None
    discount_amount: float = 0.0
    subtotal: float = 0.0
    standard_tax: float = 0.0
    liquor_tax: float = 0.0
    standard_tax_label: str = "Impuesto"
    invoice_prefix: Optional[str] = None
    invoice_number: Optional[int] = None
    invoice_cufe: Optional[str] = None
    tip_amount: float = Field(0.0, ge=0, description="Tip amount captured at checkout — shown as a separate line on the receipt (warocol.com#637).")


class AddPaymentRequest(BaseModel):
    amount: float = Field(..., gt=0, description="Amount for this payment")
    payment_method: str = Field(..., description="cash | card | digital | credit or custom slug")
    payment_method_id: Optional[str] = Field(None, description="UUID of payment_methods row")
    cash_received: Optional[float] = Field(None, description="Issue #524 — cash handed over by the customer. NULL for non-cash. Must be >= amount when set.")


class AddPaymentResponse(BaseModel):
    paid_total: float
    remaining: float
    is_complete: bool
    payment_id: str


@router.post("/receipt-email", dependencies=[Depends(require_module(Module.POS))])
async def send_receipt_email(request: Request, receipt_data: SendReceiptRequest):
    """
    Send a POS receipt email on demand.
    Used when the cashier types a customer email in the success modal after order completion.
    """
    session = require_valid_session(request)
    success = await send_pos_receipt_email(
        customer_email=receipt_data.email,
        order_number=receipt_data.order_number,
        total_amount=receipt_data.total_amount,
        payment_method=receipt_data.payment_method,
        items=receipt_data.items,
        order_date=datetime.utcnow(),
        tenant_id=str(session.tenant_id) if session and session.tenant_id else None,
        business_name=receipt_data.business_name,
        business_address=receipt_data.business_address,
        business_city=receipt_data.business_city,
        business_phone=receipt_data.business_phone,
        discount_amount=receipt_data.discount_amount,
        subtotal=receipt_data.subtotal,
        standard_tax=receipt_data.standard_tax,
        liquor_tax=receipt_data.liquor_tax,
        standard_tax_label=receipt_data.standard_tax_label,
        invoice_prefix=receipt_data.invoice_prefix,
        invoice_number=receipt_data.invoice_number,
        invoice_cufe=receipt_data.invoice_cufe,
        tip_amount=receipt_data.tip_amount,
    )
    return {"success": success}


@router.post("/{cart_id}/payments", response_model=None, dependencies=[Depends(require_module(Module.POS))])
async def add_cart_payment(
    cart_id: str,
    payment_data: AddPaymentRequest,
    request: Request,
):
    """
    Add a partial payment to a cart's order.
    Supports split payments — call multiple times until is_complete=True.
    """
    return await pos_cart_service.add_order_payment(
        request=request,
        cart_id=cart_id,
        amount=payment_data.amount,
        payment_method=payment_data.payment_method,
        payment_method_id=payment_data.payment_method_id,
        cash_received=payment_data.cash_received,
    )


class VoidPaymentRequest(BaseModel):
    reason: Optional[str] = Field(None, description="Issue warocol.com#649 — motivo opcional de la anulación (auditoría).")


@router.delete("/{cart_id}/payments/{payment_id}", dependencies=[Depends(require_module(Module.POS))])
async def void_cart_payment(
    cart_id: str,
    payment_id: str,
    body: VoidPaymentRequest,
    request: Request,
):
    """
    Issue warocol.com#649 — soft-delete a partial payment on a cart's order.
    Recomputes paid_total, reopens the cart if the void cleared the closing
    payment, and auto-reverses the posted sale journal entry atomically.
    """
    return await pos_cart_service.void_order_payment(
        request=request,
        cart_id=cart_id,
        payment_id=payment_id,
        reason=body.reason,
    )


@router.get("/{cart_id}/tax-preview", dependencies=[Depends(require_module(Module.POS))])
async def get_cart_tax_preview(
    request: Request,
    cart_id: UUID,
    discount_amount: Optional[float] = Query(
        None,
        ge=0,
        description="Issue #526 — in-flight discount applied client-side, used to compute taxes on the post-discount base.",
    ),
):
    """
    Return the running tax breakdown for an in-flight POS cart.

    Mirrors the shape of the mesa preview returned by GET /api/tables/{id}/current.
    Used by the cart sidebar in counter / bar mode so it shows live IVA / IPC
    instead of a hardcoded "Impuestos (0%)".
    """
    return await pos_cart_service.get_cart_tax_preview(
        request, cart_id, discount_amount,
    )
