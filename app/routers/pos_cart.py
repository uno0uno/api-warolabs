"""
POS Cart Router
Endpoints for managing POS cart persistence
"""
from fastapi import APIRouter, Request
from typing import List, Optional, Any, Dict
from uuid import UUID
from datetime import date, datetime
from pydantic import BaseModel, Field
from app.services import pos_cart_service
from app.services.email_helpers import send_pos_receipt_email

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


@router.post("/batch")
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


@router.get("/{customer_id}")
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


@router.post("/{cart_id}/items")
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


@router.put("/{cart_id}/items/{item_id}")
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


@router.delete("/{cart_id}/items/{item_id}")
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


@router.delete("/{cart_id}")
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
    receipt_email: Optional[str] = Field(None, description="Optional customer email to send receipt to after order completes")
    discount_type: Optional[str] = Field(None, description="'percent' | 'fixed'")
    discount_value: Optional[float] = Field(None, description="10 for 10%, 5000 for $5,000 COP")


@router.post("/{cart_id}/complete")
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
    )


class SendReceiptRequest(BaseModel):
    email: str = Field(..., description="Customer email to send receipt to")
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


@router.post("/receipt-email")
async def send_receipt_email(receipt_data: SendReceiptRequest):
    """
    Send a POS receipt email on demand.
    Used when the cashier types a customer email in the success modal after order completion.
    """
    success = await send_pos_receipt_email(
        customer_email=receipt_data.email,
        order_number=receipt_data.order_number,
        total_amount=receipt_data.total_amount,
        payment_method=receipt_data.payment_method,
        items=receipt_data.items,
        order_date=datetime.utcnow(),
        business_name=receipt_data.business_name,
        business_address=receipt_data.business_address,
        business_city=receipt_data.business_city,
        business_phone=receipt_data.business_phone,
        discount_amount=receipt_data.discount_amount,
        subtotal=receipt_data.subtotal,
    )
    return {"success": success}
