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


@router.post("/{cart_id}/complete")
async def complete_order(
    request: Request,
    cart_id: UUID,
    order_data: CompleteOrderRequest
):
    """
    Complete POS order:
    - Creates order record
    - Copies cart items to order_items
    - Updates inventory
    - Marks cart as completed
    """
    return await pos_cart_service.complete_pos_order(
        request,
        cart_id,
        order_data.payment_method
    )
