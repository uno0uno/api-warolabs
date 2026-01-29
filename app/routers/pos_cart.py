"""
POS Cart Router
Endpoints for managing POS cart persistence
"""
from fastapi import APIRouter, Request, Body
from typing import List, Optional
from uuid import UUID
from pydantic import BaseModel, Field
from app.services import pos_cart_service

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
    payment_method: str = Field(..., description="Payment method: cash, card, digital")
    customer_id: UUID = Field(..., description="Customer ID to associate with the order")


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
        order_data.customer_id
    )
