"""
Online Cart Models
Pydantic models for online ordering system (public endpoints)
"""
from pydantic import BaseModel, Field, EmailStr
from typing import List, Optional
from uuid import UUID
from datetime import datetime
from decimal import Decimal


class ModifierInput(BaseModel):
    """Modifier input for cart items — only the ID is required.
    Price and name are always looked up from the DB."""
    id: UUID


class OnlineCartItemCreate(BaseModel):
    """Create cart item request — unit_price is looked up from DB by product_id."""
    product_id: UUID
    quantity: int = Field(gt=0)
    modifiers: List[ModifierInput] = []
    notes: Optional[str] = None


class OnlineCartItemModifier(BaseModel):
    """Cart item modifier response"""
    id: UUID
    modifier_id: UUID
    modifier_name: str
    price: Decimal


class OnlineCartItem(BaseModel):
    """Cart item response"""
    id: UUID
    product_id: UUID
    product_name: str
    quantity: int
    unit_price: Decimal
    subtotal: Decimal
    modifiers: List[OnlineCartItemModifier]
    notes: Optional[str]


class OnlineCartCreate(BaseModel):
    """Create online cart with batch items"""
    items: List[OnlineCartItemCreate]
    session_id: Optional[str] = None
    order_type: str = Field(default='delivery', pattern='^(delivery|pickup|dine-in)$')


class DeliveryInfoUpdate(BaseModel):
    """Update delivery information"""
    order_type: str = Field(pattern='^(delivery|pickup|dine-in)$')
    delivery_address_id: Optional[UUID] = None
    scheduled_time: Optional[datetime] = None
    delivery_instructions: Optional[str] = None


class OnlineCartResponse(BaseModel):
    """Online cart response"""
    id: UUID
    tenant_id: UUID
    customer_id: Optional[UUID]
    session_id: Optional[str]
    order_type: str
    delivery_address_id: Optional[UUID]
    scheduled_time: Optional[datetime]
    delivery_instructions: Optional[str]
    pickup_pin: Optional[str]
    is_verified: bool
    verified_email: Optional[str]
    status: str
    total_amount: Decimal
    items: List[OnlineCartItem]
    created_at: datetime
    updated_at: datetime


class CheckoutResponse(BaseModel):
    """Checkout response after cart is confirmed as order"""
    order_id: UUID
    order_number: int
    total_amount: Decimal
    order_type: str
    pickup_pin: Optional[str] = None
    estimated_preparation_time: Optional[int] = None


class AddressCreate(BaseModel):
    """Create address for delivery"""
    address_line1: str
    address_line2: Optional[str] = None
    city: str
    state: Optional[str] = None
    postal_code: Optional[str] = None
    country: str = 'Colombia'
    phone_number: Optional[str] = None
    delivery_notes: Optional[str] = None
    label: Optional[str] = None  # Casa|Trabajo|Otro
    is_default: bool = False


class AddressResponse(BaseModel):
    """Address response"""
    id: UUID
    user_id: UUID
    address_line1: str
    address_line2: Optional[str]
    city: str
    state: Optional[str]
    postal_code: Optional[str]
    country: str
    phone_number: Optional[str]
    delivery_notes: Optional[str]
    is_validated: bool
    delivery_zone: Optional[str]
    is_default: bool
    label: Optional[str]
    created_at: datetime


class OnlineOrderItem(BaseModel):
    """Single online order row for restaurant management view"""
    id: str
    order_number: int
    order_date: str
    scheduled_time: Optional[str]
    total_amount: float
    status: str
    order_type: str
    delivery_instructions: Optional[str]
    verified_email: Optional[str]


class OnlineOrdersResponse(BaseModel):
    """Paginated response for GET /online/orders/"""
    success: bool
    data: List[OnlineOrderItem]
    pagination: dict
